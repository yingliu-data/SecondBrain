"""local — the guest harness as a subprocess (tests, no-KVM machines).

Runs `python -m guest.main --connect unix:{session_dir}/ipc --log-dir
{session_dir}/guest --workspace {session_dir}/workspace` with the harness
directory on its path. The guest then connects to the same
`{session_dir}/ipc/v.sock_{port}` listeners Firecracker would (Endpoints).

M3 emulation of snapshots (docs/M3-CONTRACTS.md §2–4):

  restore(session_dir)             BASE: a fresh guest process (= start()); the
                                   worker sends hello, or hello{resume} after a death
  snapshot(snapshot_dir)           after `parked` the guest sits in its idle loop
                                   keeping its in-memory state; the process group
                                   is frozen with SIGSTOP (the analogue of a paused,
                                   snapshotted VM) and a marker
                                   `{snapshot_dir}/LOCAL_SNAPSHOT.json` {pid} is
                                   written. If the guest already exited there is no
                                   state to keep: SandboxError, no marker.
  restore(session_dir, snapdir)    SIGCONT the frozen process: same memory, same
                                   seq; its idle loop reconnects. SandboxError when
                                   the process is gone (the worker falls back to
                                   the death path, §4). The marker is consumed.
  has_snapshot(None)               True: the "base" is the guest code itself
  has_snapshot(snapdir)            marker present and its process still frozen

After snapshot(), is_alive() is False (as for a snapshotted Firecracker VM);
stop() kills a frozen process too.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from app.config import HARNESS_GUEST_ROOT
from app.host.sandbox import Sandbox

logger = logging.getLogger("host.sandbox")

LOCAL_SNAPSHOT_MARKER = "LOCAL_SNAPSHOT.json"


def _sandbox_error(msg: str) -> Exception:
    # The worker catches SandboxError from either backend; the class lives in
    # firecracker.py (stdlib-only, safe to import here).
    from app.host.sandbox.firecracker import SandboxError
    return SandboxError(msg)


class _Frozen:
    """A stopped guest process group adopted by pid (Popen-like subset)."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self.returncode = -1
        except PermissionError:
            pass
        else:
            if _is_zombie(self.pid):
                self.returncode = -1
        return self.returncode

    def terminate(self) -> None:
        _signal_group(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        _signal_group(self.pid, signal.SIGKILL)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("guest", timeout)
            time.sleep(0.02)
        return self.returncode  # type: ignore[return-value]


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


class LocalSandbox(Sandbox):
    def __init__(self, guest_root: str | Path = HARNESS_GUEST_ROOT,
                 python: str | None = None, extra_args: list[str] | None = None,
                 snapshot_grace_s: float = 0.0):
        self.guest_root = Path(guest_root)
        self.python = python or sys.executable
        self.extra_args = list(extra_args or [])
        self.snapshot_grace_s = snapshot_grace_s   # wait before freezing (idle loop settles)
        self.proc: subprocess.Popen | None = None
        self.session_dir: Path | None = None
        self._log = None
        self._frozen = None          # Popen or _Frozen while snapshotted
        self.last_restore_ms: float | None = None
        self.last_snapshot_ms: float | None = None
        self.last_vm_event: dict | None = None

    def start(self, session_dir: Path) -> None:
        if self.is_alive():
            return
        session_dir = Path(session_dir)
        self.session_dir = session_dir
        log_dir = session_dir / "guest"
        log_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "workspace").mkdir(parents=True, exist_ok=True)
        cmd = [
            self.python, "-m", "guest.main",
            "--connect", f"unix:{session_dir / 'ipc'}",
            "--log-dir", str(log_dir),
            "--workspace", str(session_dir / "workspace"),
            *self.extra_args,
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.guest_root) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._log = open(log_dir / "guest.stderr.log", "ab")
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.guest_root), env=env,
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logger.info("guest started pid=%s (%s)", self.proc.pid, " ".join(cmd))

    def stop(self) -> None:
        frozen, self._frozen = self._frozen, None
        if frozen is not None and frozen.poll() is None:
            frozen.kill()                       # SIGKILL works on a stopped process
            _signal_group(frozen.pid, signal.SIGCONT)
            try:
                frozen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    # --------------------------------------------------------------- M3

    def restore(self, session_dir: Path, snapshot_dir: Path | None = None) -> None:
        if self.is_alive():
            raise _sandbox_error("sandbox already started")
        t0 = time.monotonic()
        if snapshot_dir is None:
            self.stop()                          # drop any stale frozen process
            self.start(session_dir)
            source = "base"
        else:
            marker = Path(snapshot_dir) / LOCAL_SNAPSHOT_MARKER
            frozen = self._frozen_for(Path(snapshot_dir))
            if frozen is None:
                raise _sandbox_error(f"no local snapshot in {snapshot_dir} (process gone)")
            _signal_group(frozen.pid, signal.SIGCONT)
            self.proc, self._frozen = frozen, None   # type: ignore[assignment]
            self.session_dir = Path(session_dir)
            try:
                marker.unlink()                  # single-use, like a Firecracker session snapshot
            except FileNotFoundError:
                pass
            source = "session"
        self.last_restore_ms = round((time.monotonic() - t0) * 1000, 1)
        self.last_vm_event = {"op": "restore", "source": source, "ms": self.last_restore_ms}

    def snapshot(self, snapshot_dir: Path) -> None:
        t0 = time.monotonic()
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise _sandbox_error("local snapshot: the guest process has exited; no state to keep")
        if self.snapshot_grace_s:
            time.sleep(self.snapshot_grace_s)
        _signal_group(proc.pid, signal.SIGSTOP)
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_dir / (LOCAL_SNAPSHOT_MARKER + ".tmp")
        tmp.write_text(json.dumps({"pid": proc.pid, "created_ms": int(time.time() * 1000)}))
        os.replace(tmp, snapshot_dir / LOCAL_SNAPSHOT_MARKER)
        self._frozen, self.proc = proc, None
        self.last_snapshot_ms = round((time.monotonic() - t0) * 1000, 1)
        self.last_vm_event = {"op": "snapshot", "source": "session", "ms": self.last_snapshot_ms}

    def has_snapshot(self, snapshot_dir: Path | None = None) -> bool:
        if snapshot_dir is None:
            return True
        return self._frozen_for(Path(snapshot_dir)) is not None

    def _frozen_for(self, snapshot_dir: Path):
        """The frozen process the marker in `snapshot_dir` names, if alive.
        Adopts it by pid when this sandbox instance did not freeze it (e.g.
        a new worker object for the same session)."""
        try:
            pid = int(json.loads((snapshot_dir / LOCAL_SNAPSHOT_MARKER).read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if self._frozen is not None and self._frozen.pid == pid:
            return self._frozen if self._frozen.poll() is None else None
        cand = _Frozen(pid)
        if cand.poll() is not None or "guest.main" not in _cmdline(pid):
            return None
        return cand
