"""guest.ops — the operation manager: runs guest-side tools off the coordinator
thread and posts their results on the coordinator's inbound queue.

Contract (docs/GUEST.md):
  OperationManager(workspace, limits, inbound: queue.Queue, pool_size=4, bulk=None)
    submit(call_id, name, args) -> None   run the tool; posts
        ("op", call_id, result) with result = {"ok": bool, "content": str,
        "state": "done|failed|timeout|killed", "duration_ms": int,
        "spill": None | {"path": str, "sha256": str, "bytes": int}}
    abort_all() -> set[call_id]           SIGKILL every running process group,
        drop queued ops; all of them are reported "killed"
    running() -> set[call_id]             submitted and not yet reported
    spill_threshold: int
    workspace_tree_id() -> str            deterministic hash of the workspace (cached)
    shutdown(wait=False)                  tear down the worker pool (tests)
  workspace_tree_id(workspace) -> str     the module-level form: uncached, the
                                          reference the cached one must equal

Tree id cache: the manager keeps `relative path -> ((st_mtime_ns, st_size,
st_ino), digest)` and re-hashes only files whose stat key changed (entries
that disappeared are dropped). It returns the previous id without walking at
all while no operation has finished since the last call, so a change made
behind the manager's back (not by one of its operations) shows up only after
the next operation finishes. A rewrite that keeps size, mtime_ns and inode is
not detected by the cache; the id is still a pure function of the tree
whenever every file's stat key changes with its content.

Threading: a pool of `pool_size` worker threads runs file tools concurrently.
`write`, `edit` and `apply_patch` on the same path run serially, in
submission order (guest.tools.SERIALISED); every other op runs as soon as a
worker is free. Queued serialised ops do not occupy a worker.

bash: `subprocess.Popen(start_new_session=True)`, stdout+stderr combined,
cwd = workspace, timeout = args.timeout_s or limits.op_timeout_s["guest"].
On timeout the whole process group gets SIGKILL and the op is "timeout";
after abort_all it is "killed". A non-zero exit is ok=False, state "failed".

Spill: content longer than `limits.spill_threshold` chars is written to
`{workspace}/.harness/spill/{call_id}.txt`; `content` becomes the first
`spill_threshold` chars plus a `[truncated: N chars; spill {call_id}]` line
and `spill` carries path/sha256/bytes. `limits.spill_quota_bytes` bounds the
total bytes spilled in the session (files already in the spill dir count);
over quota no file is written and the note says so.

M3: with a `bulk` client (guest.bulk.BulkClient) every spill file is also put
on :5001 as kind "spill", path `.spill/{call_id}.txt` (protocol.bulk.spill_path),
from the operation's worker thread before the result is posted; `spill` then
carries `bulk_path` (that path). An upload failure is logged only. The
coordinator records the `bulk` event and `spill_ref` with `bulk_path`.

M4 (docs/GUEST.md "M4"): mcp-side calls run here too, each on its own
daemon thread (a gateway call may take many minutes; it must not hold a pool
worker), through the guest.mcp client:
  submit_mcp(call_id, name, args, *, attempt=0, input_response=None,
             timeout_s=None) -> None
      one POST /mcp tools/call. Its outcome is posted as ("op", call_id,
      result) like any operation (text -> content, isError -> ok False /
      "failed", a gateway or connection error -> "failed", a socket timeout
      -> "timeout", abort -> "killed"; spill applies), or, when the gateway
      asks for input, as ("op_input", call_id, input_required) — the job is
      then dropped (the operation is suspended: no thread, no connection) and
      the coordinator re-submits it with the user's answer (attempt + 1).
      An isError text gets the model-visible `error: ` prefix here
      (error_content: the one place, unless it already starts with "error:").
  detach_mcp() -> set[call_id]  park: close every in-flight mcp connection
      and forget the jobs without reporting (the forwarder holds the calls;
      the coordinator re-attaches with submit_mcp(..., reattach=True)). A
      re-attach answered 410 is reported state "lost".
  cancel(call_id)  drop a running mcp job without reporting it (the
      coordinator's op timeout already recorded the outcome); closes its
      connection.
  send_mcp(call_id, name, args, *, attempt, input_response) fire-and-forget
      re-send whose result nobody waits for (a cancel when the task ends;
      socket timeout RESEND_TIMEOUT_S).
abort_all() closes every in-flight mcp connection (state "killed").

Only `inbound.put` touches the coordinator; `State` is never read here.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import signal
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from guest import tools
from guest.mcp import GONE, McpAborted, McpError
from guest.tools import builtin
from protocol.bulk import spill_path as bulk_spill_path

log = logging.getLogger("guest.ops")

HARNESS_DIR = builtin.HARNESS_DIR     # the guest's own top-level dir (spill, todo.json)
LOCAL_SPILL_DIR = "spill"             # local spill copies: {workspace}/.harness/spill/
DEFAULT_SPILL_THRESHOLD = 2000
DEFAULT_SPILL_QUOTA = 50_000_000
DEFAULT_OP_TIMEOUT = 120.0
RESEND_TIMEOUT_S = 30.0               # send_mcp: a fire-and-forget re-send nobody waits for
LOST_MCP = "error: operation lost (the gateway forwarder no longer holds this call)"
KILLED, DROPPED = "killed", "dropped"  # _Job.stop: abort_all (report "killed") / cancel, detach (report nothing)


def error_content(text: str) -> str:
    """The model-visible text of a failed gateway result: the one place the
    `error: ` prefix is added (unless the text already carries one)."""
    return text if text[:6].lower() == "error:" else f"error: {text}"


@dataclass
class _Job:
    call_id: str
    name: str
    args: dict
    keys: frozenset[str]              # paths this op serialises on
    submitted: float
    proc: subprocess.Popen | None = None
    stop: str | None = None           # None, KILLED or DROPPED
    held: bool = False                # keys are in OperationManager._busy
    mcp: object | None = None         # the in-flight guest.mcp.McpCall
    mcp_job: bool = False             # an mcp call (submit_mcp)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _serial_keys(name: str, args: dict) -> frozenset[str]:
    """The workspace paths an op must hold exclusively: write/edit's `path`;
    every file a patch touches (or a single catch-all key when it cannot be
    parsed, so the error still reports in order)."""
    if name not in tools.SERIALISED:
        return frozenset()
    if name == "apply_patch":
        try:
            files = builtin.parse_patch(str(args.get("patch", "")))
        except ValueError:
            return frozenset({"<patch>"})
        keys = set()
        for f in files:
            keys.add(f["new"] if f["new"] != "/dev/null" else f["old"])
        return frozenset(keys) or frozenset({"<patch>"})
    path = args.get("path")
    return frozenset({os.path.normpath(path)}) if isinstance(path, str) else frozenset({"<path>"})


def workspace_tree_id(workspace: Path) -> str:
    """sha256 over `relative_path\\0sha256(file bytes)\\n` for every regular
    file under `workspace`, sorted by path, excluding `.harness/`. Symlinks
    contribute their target string instead of file bytes. Deterministic across
    runs; changes whenever any file's path or content changes."""
    workspace = Path(workspace)
    h = hashlib.sha256()
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if not (
            Path(dirpath) == workspace and d == HARNESS_DIR))
        for name in filenames:
            p = Path(dirpath) / name
            entries.append((p.relative_to(workspace).as_posix(), p))
    for rel, p in sorted(entries):
        if p.is_symlink():
            digest = _symlink_digest(p)
        elif p.is_file():
            digest = _file_digest(p)
            if digest is None:
                continue
        else:
            continue
        h.update(rel.encode("utf-8") + b"\0" + digest.encode() + b"\n")
    return h.hexdigest()


def _file_digest(path) -> str | None:
    """sha256 hex of a file's bytes; None when it cannot be read."""
    fh = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                fh.update(chunk)
    except OSError:
        return None
    return fh.hexdigest()


def _symlink_digest(path) -> str:
    return hashlib.sha256(b"symlink:" + os.readlink(path).encode()).hexdigest()


class _TreeCache:
    """workspace_tree_id with per-file digests cached by stat key (module
    doc). The walk matches workspace_tree_id(): regular files and symlinks
    (not followed; a symlink to a directory is skipped, as os.walk does),
    `.harness/` excluded at the top level."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._files: dict[str, tuple[tuple, str]] = {}

    def tree_id(self) -> str:
        seen: dict[str, tuple[tuple, str]] = {}
        self._scan(str(self.workspace), "", seen)
        self._files = seen
        h = hashlib.sha256()
        for rel in sorted(seen):
            h.update(rel.encode("utf-8") + b"\0" + seen[rel][1].encode() + b"\n")
        return h.hexdigest()

    def _scan(self, directory: str, prefix: str, seen: dict) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            rel = prefix + entry.name
            try:
                if entry.is_dir():
                    if not entry.is_symlink() and not (prefix == "" and entry.name == HARNESS_DIR):
                        self._scan(entry.path, rel + "/", seen)
                    continue
                link = entry.is_symlink()
                if not link and not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
            cached = self._files.get(rel)
            if cached is not None and cached[0] == key:
                seen[rel] = cached
                continue
            try:
                digest = _symlink_digest(entry.path) if link else _file_digest(entry.path)
            except OSError:
                digest = None
            if digest is not None:
                seen[rel] = (key, digest)


class OperationManager:
    def __init__(self, workspace, limits, inbound: queue.Queue, pool_size: int = 4, bulk=None,
                 mcp=None):
        self.workspace = Path(workspace)
        self.bulk = bulk
        self.mcp = mcp                            # guest.mcp.McpClient (M4)
        self.limits = limits
        self.inbound = inbound
        self.spill_threshold = int(getattr(limits, "spill_threshold", DEFAULT_SPILL_THRESHOLD))
        self.spill_quota_bytes = int(getattr(limits, "spill_quota_bytes", DEFAULT_SPILL_QUOTA))
        self._pool = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="op")
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}          # every unreported op
        self._pending: list[_Job] = []            # serialised ops waiting for a path
        self._busy: set[str] = set()              # paths held by running ops
        self._spill_used = self._existing_spill_bytes()
        self._finished = 0                        # operations reported so far
        self._tree = _TreeCache(self.workspace)
        self._tree_lock = threading.Lock()
        self._tree_id: str | None = None
        self._tree_at = -1                        # self._finished when _tree_id was computed

    # ------------------------------------------------------------- public

    def submit(self, call_id: str, name: str, args: dict) -> None:
        """Queue one guest-side tool call. Never blocks, never raises for
        bad input: an unknown tool, a question or invalid args are reported
        as a failed result."""
        job = _Job(call_id=call_id, name=name, args=args if isinstance(args, dict) else {},
                   keys=_serial_keys(name, args if isinstance(args, dict) else {}),
                   submitted=time.monotonic())
        with self._lock:
            if call_id in self._jobs:
                self._post(call_id, False, f"Error: duplicate call_id {call_id!r}", "failed",
                           job.submitted, None)
                return
            self._jobs[call_id] = job
            errors = tools.validate_args(name, args)
            spec = tools.SPECS.get(name)
            runnable = spec is not None and (spec.impl is not None or name == "bash")
            if errors or not runnable:
                if errors:
                    reason = "; ".join(errors)
                elif spec is not None and spec.is_question:
                    reason = "handled by the coordinator, not an operation"
                else:
                    reason = f"unknown tool {name!r}"
                self._pool.submit(self._report_immediate, job,
                                  f"Error: invalid call to {name}: {reason}")
                return
            if job.keys:
                self._pending.append(job)
                self._dispatch_locked()
            else:
                self._pool.submit(self._run, job)

    def abort_all(self) -> set[str]:
        """SIGKILL every running process group, drop queued ops; each is
        reported with state "killed" (running file ops when they finish)."""
        with self._lock:
            jobs = list(self._jobs.values())
            queued = list(self._pending)
            self._pending.clear()
        aborted = set()
        for job in jobs:
            with job.lock:
                job.stop = job.stop or KILLED
                proc, call = job.proc, job.mcp
            aborted.add(job.call_id)
            if proc is not None:
                _killpg(proc)
            if call is not None:
                call.close()
        for job in queued:
            self._pool.submit(self._report_immediate, job, "Error: aborted before it ran")
        return aborted

    # ------------------------------------------------------------ mcp (M4)

    def submit_mcp(self, call_id: str, name: str, args: dict, *, attempt: int = 0,
                   input_response: dict | None = None, timeout_s=None,
                   reattach: bool = False) -> None:
        job = _Job(call_id=call_id, name=name, args=args if isinstance(args, dict) else {},
                   keys=frozenset(), submitted=time.monotonic(), mcp_job=True)
        with self._lock:
            if call_id in self._jobs:
                self._post(call_id, False, f"Error: duplicate call_id {call_id!r}", "failed",
                           job.submitted, None)
                return
            self._jobs[call_id] = job
        if self.mcp is None:
            self._finish(job, False, "error: no gateway client in this guest", "failed")
            return
        threading.Thread(target=self._run_mcp, args=(job, attempt, input_response, timeout_s, reattach),
                         name=f"mcp-{call_id}", daemon=True).start()

    def cancel(self, call_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(call_id, None)
        if job is not None:
            self._drop(job)

    def send_mcp(self, call_id: str, name: str, args: dict, *, attempt: int,
                 input_response: dict | None = None) -> None:
        if self.mcp is None:
            return

        def run():
            try:
                self.mcp.call_tool(call_id, name, args, attempt=attempt,
                                   input_response=input_response, timeout_s=RESEND_TIMEOUT_S)
            except Exception as e:   # noqa: BLE001 - nobody waits for this one
                log.info("mcp re-send for %s (attempt %d) failed: %s", call_id, attempt, e)
        threading.Thread(target=run, name=f"mcp-send-{call_id}", daemon=True).start()

    def detach_mcp(self) -> set[str]:
        """Park (M4-CONTRACTS 3.3): close every in-flight mcp connection and
        forget the jobs without reporting them; the host forwarder keeps the
        calls and the coordinator re-attaches them after the resume."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.mcp_job]
            for j in jobs:
                del self._jobs[j.call_id]
        for j in jobs:
            self._drop(j)
        return {j.call_id for j in jobs}

    @staticmethod
    def _drop(job: _Job) -> None:
        """Forget a job (already out of _jobs): it reports nothing; its
        connection is closed."""
        with job.lock:
            job.stop = DROPPED
            call = job.mcp
        if call is not None:
            call.close()

    def _run_mcp(self, job: _Job, attempt: int, input_response, timeout_s, reattach=False) -> None:
        result = error = None
        try:
            call = self.mcp.start(job.call_id, job.name, job.args, attempt=attempt,
                                  input_response=input_response, reattach=reattach,
                                  timeout_s=float(timeout_s) if timeout_s else None)
            with job.lock:
                stopped = job.stop is not None
                job.mcp = call
            if stopped:
                raise McpAborted("aborted")
            result = call.run()
        except McpError as e:
            error = e
        except Exception as e:   # noqa: BLE001 - a client bug must not lose the call
            error = McpError(f"internal error in the mcp client: {e!r}")
        if job.stop == DROPPED:
            return
        if job.stop is None and error is None and result.input_required is not None:
            with self._lock:
                if self._jobs.get(job.call_id) is job:
                    del self._jobs[job.call_id]
            self.inbound.put(("op_input", job.call_id, result.input_required))
            return
        if job.stop == KILLED:
            ok, content, state = False, "error: aborted", "killed"
        elif error is not None:
            ok = False
            content = LOST_MCP if error.status == GONE else f"error: {error.message}"
            state = "lost" if error.status == GONE else "timeout" if error.timed_out else "failed"
        elif result.is_error:
            ok, content, state = False, error_content(result.text), "failed"
        else:
            ok, content, state = True, result.text, "done"
        self._finish(job, ok, content, state)

    def running(self) -> set[str]:
        with self._lock:
            return set(self._jobs)

    def workspace_tree_id(self) -> str:
        """The workspace's tree id, re-hashing only changed files, and not
        walking at all when no operation finished since the last call."""
        with self._tree_lock:
            with self._lock:
                finished = self._finished
            if self._tree_id is None or finished != self._tree_at:
                self._tree_id = self._tree.tree_id()
                self._tree_at = finished
            return self._tree_id

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

    # ------------------------------------------------------------ workers

    def _dispatch_locked(self) -> None:
        """Start every queued serialised op whose paths are free, preserving
        per-path submission order. Caller holds self._lock."""
        blocked: set[str] = set()
        still: list[_Job] = []
        for job in self._pending:
            if job.keys & (self._busy | blocked):
                blocked |= job.keys
                still.append(job)
            else:
                self._busy |= job.keys
                job.held = True
                self._pool.submit(self._run, job)
        self._pending = still

    def _report_immediate(self, job: _Job, message: str) -> None:
        state = "killed" if job.stop == KILLED else "failed"
        self._finish(job, False, message, state)

    def _run(self, job: _Job) -> None:
        try:
            if job.stop == KILLED:
                self._finish(job, False, "Error: aborted before it ran", "killed")
                return
            if job.name == "bash":
                ok, content, state = self._run_bash(job)
            else:
                impl = tools.impl_for(job.name)
                content = impl(self.workspace, job.args, self.limits)
                if not isinstance(content, str):
                    content = str(content)
                ok = not content.startswith("Error:")
                state = "done" if ok else "failed"
            if job.stop == KILLED:
                ok, state = False, "killed"
            self._finish(job, ok, content, state)
        except Exception as e:   # programming error in a tool: never kill the worker
            tb = traceback.format_exc(limit=3)
            self._finish(job, False, f"Error: internal error in {job.name}: {e!r}\n{tb}", "failed")

    def _run_bash(self, job: _Job) -> tuple[bool, str, str]:
        argv, timeout = builtin.bash_command(job.args, self.limits)
        with job.lock:
            if job.stop == KILLED:
                return False, "Error: aborted before it ran", "killed"
            proc = subprocess.Popen(
                argv, cwd=str(self.workspace), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            job.proc = proc
        state = "done"
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(proc)
            out = _reap(proc)
            state = "timeout"
        text = (out or b"").decode("utf-8", errors="replace")
        if job.stop == KILLED:
            return False, _note(text, "[killed: aborted]"), "killed"
        if state == "timeout":
            return False, _note(text, f"[timeout after {timeout:g}s: process group killed]"), "timeout"
        if proc.returncode != 0:
            return False, _note(text, f"[exit code {proc.returncode}]"), "failed"
        return True, text, "done"

    # ------------------------------------------------------------- finish

    def _finish(self, job: _Job, ok: bool, content: str, state: str) -> None:
        content, spill = self._spill(job.call_id, content)
        with self._lock:
            self._jobs.pop(job.call_id, None)
            self._finished += 1                   # the workspace may have changed
            if job.held:
                job.held = False
                self._busy -= job.keys
                self._dispatch_locked()
        self._post(job.call_id, ok, content, state, job.submitted, spill)

    def _post(self, call_id, ok, content, state, started, spill) -> None:
        result = {
            "ok": bool(ok),
            "content": content,
            "state": state,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "spill": spill,
        }
        self.inbound.put(("op", call_id, result))

    # -------------------------------------------------------------- spill

    def _existing_spill_bytes(self) -> int:
        d = self.workspace / HARNESS_DIR / LOCAL_SPILL_DIR
        if not d.is_dir():
            return 0
        return sum(p.stat().st_size for p in d.iterdir() if p.is_file())

    def _spill(self, call_id: str, content: str) -> tuple[str, dict | None]:
        if len(content) <= self.spill_threshold:
            return content, None
        head = content[: self.spill_threshold]
        dropped = len(content) - len(head)
        data = content.encode("utf-8")
        with self._lock:
            over = self._spill_used + len(data) > self.spill_quota_bytes
            if not over:
                self._spill_used += len(data)
        if over:
            return head + f"\n[truncated: {dropped} chars; spill quota exhausted, full output lost]", None
        spill_dir = self.workspace / HARNESS_DIR / LOCAL_SPILL_DIR
        path = spill_dir / f"{call_id}.txt"
        try:
            spill_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:
            with self._lock:
                self._spill_used -= len(data)
            return head + f"\n[truncated: {dropped} chars; spill failed: {e.strerror or e}]", None
        spill = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if self.bulk is not None:
            spill["bulk_path"] = bulk_spill_path(f"{call_id}.txt")
            try:
                self.bulk.put("spill", spill["bulk_path"], data, sha256=spill["sha256"])
            except Exception as e:   # noqa: BLE001 - the local file is still there
                log.warning("spill upload of %s failed: %s", call_id, e)
        return head + f"\n[truncated: {dropped} chars; spill {call_id}]", spill


def _note(text: str, note: str) -> str:
    """Append a status line to command output on its own line."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + note


def _reap(proc: subprocess.Popen, grace_s: float = 2.0) -> bytes:
    """Collect a killed process's output without waiting forever: a daemonised
    grandchild (setsid, nohup) can keep the stdout pipe open after the group is
    dead, so the read is bounded and the pipe is closed instead."""
    try:
        out, _ = proc.communicate(timeout=grace_s)
        return out or b""
    except subprocess.TimeoutExpired:
        pass
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
    return b""


def _killpg(proc: subprocess.Popen) -> None:
    """SIGKILL the process group `proc` leads; silent when already gone."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
