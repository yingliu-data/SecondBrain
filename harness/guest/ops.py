"""guest.ops — the operation manager: runs guest-side tools off the coordinator
thread and posts their results on the coordinator's inbound queue.

Contract (docs/GUEST.md):
  OperationManager(workspace, limits, inbound: queue.Queue, pool_size=4)
    submit(call_id, name, args) -> None   run the tool; posts
        ("op", call_id, result) with result = {"ok": bool, "content": str,
        "state": "done|failed|timeout|killed", "duration_ms": int,
        "spill": None | {"path": str, "sha256": str, "bytes": int}}
    abort_all() -> set[call_id]           SIGKILL every running process group,
        drop queued ops; all of them are reported "killed"
    running() -> set[call_id]             submitted and not yet reported
    spill_threshold: int
    workspace_tree_id() -> str            deterministic hash of the workspace
    shutdown(wait=False)                  tear down the worker pool (tests)
  workspace_tree_id(workspace) -> str     the module-level form

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

Only `inbound.put` touches the coordinator; `State` is never read here.
"""
from __future__ import annotations

import hashlib
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
from guest.tools import builtin

HARNESS_DIR = ".harness"
SPILL_DIR = "spill"
DEFAULT_SPILL_THRESHOLD = 2000
DEFAULT_SPILL_QUOTA = 50_000_000
DEFAULT_OP_TIMEOUT = 120.0


@dataclass
class _Job:
    call_id: str
    name: str
    args: dict
    keys: frozenset[str]              # paths this op serialises on
    submitted: float
    proc: subprocess.Popen | None = None
    killed: bool = False
    held: bool = False                # keys are in OperationManager._busy
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
            digest = hashlib.sha256(b"symlink:" + os.readlink(p).encode()).hexdigest()
        elif p.is_file():
            fh = hashlib.sha256()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 16), b""):
                        fh.update(chunk)
            except OSError:
                continue
            digest = fh.hexdigest()
        else:
            continue
        h.update(rel.encode("utf-8") + b"\0" + digest.encode() + b"\n")
    return h.hexdigest()


class OperationManager:
    def __init__(self, workspace, limits, inbound: queue.Queue, pool_size: int = 4):
        self.workspace = Path(workspace)
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
                job.killed = True
                proc = job.proc
            aborted.add(job.call_id)
            if proc is not None:
                _killpg(proc)
        for job in queued:
            self._pool.submit(self._report_immediate, job, "Error: aborted before it ran")
        return aborted

    def running(self) -> set[str]:
        with self._lock:
            return set(self._jobs)

    def workspace_tree_id(self) -> str:
        return workspace_tree_id(self.workspace)

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
        state = "killed" if job.killed else "failed"
        self._finish(job, False, message, state)

    def _run(self, job: _Job) -> None:
        try:
            if job.killed:
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
            if job.killed:
                ok, state = False, "killed"
            self._finish(job, ok, content, state)
        except Exception as e:   # programming error in a tool: never kill the worker
            tb = traceback.format_exc(limit=3)
            self._finish(job, False, f"Error: internal error in {job.name}: {e!r}\n{tb}", "failed")

    def _run_bash(self, job: _Job) -> tuple[bool, str, str]:
        argv, timeout = builtin.bash_command(job.args, self.limits)
        with job.lock:
            if job.killed:
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
        if job.killed:
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
        d = self.workspace / HARNESS_DIR / SPILL_DIR
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
        spill_dir = self.workspace / HARNESS_DIR / SPILL_DIR
        path = spill_dir / f"{call_id}.txt"
        try:
            spill_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:
            with self._lock:
                self._spill_used -= len(data)
            return head + f"\n[truncated: {dropped} chars; spill failed: {e.strerror or e}]", None
        spill = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
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
