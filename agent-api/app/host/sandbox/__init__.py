"""sandbox — where the guest harness runs.

`Sandbox` is the interface the worker drives. Two backends:

  firecracker.py  FirecrackerSandbox: a microVM per session; vsock uds_path
                  = {session_dir}/ipc/v.sock, so guest-initiated connections
                  to port P land on {session_dir}/ipc/v.sock_P (Endpoints)
  local.py        LocalSandbox: the guest as a subprocess connecting to the
                  same Unix sockets (--connect unix:{session_dir}/ipc);
                  tests and the no-KVM build machine

Standard library only: firecracker.py imports this module (and falls back
when it is loaded standalone by harness/image/build_snapshot.py).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class SandboxError(RuntimeError):
    """Launch, API, snapshot or lifecycle failure. API failures carry the
    backend's response body verbatim (`status`, `body`)."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Sandbox(ABC):
    """One guest runtime. `start` boots the guest for a session directory;
    the guest then connects out to the session's endpoints. `stop` kills it.
    A sandbox instance belongs to one session and may be started again after
    a stop (a new guest run).

    `last_vm_event` is {op, source, ms} of the last start/restore/snapshot/
    stop as the backend measured it (the worker's `vm` host event), or None."""

    last_vm_event: dict | None = None

    @abstractmethod
    def start(self, session_dir: Path) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_alive(self) -> bool: ...

    # --- snapshots ------------------------------------------------------------
    # Default implementations: no snapshots (restore raises SandboxError, so
    # the worker falls back to start()).

    def restore(self, session_dir: Path, snapshot_dir: Path | None = None) -> None:
        """Start the guest for `session_dir` from a snapshot instead of a cold
        boot. `snapshot_dir=None` → the shared BASE snapshot (harness idle
        before connecting out); otherwise a per-session snapshot previously
        written by `snapshot()` (normally `{session_dir}/vm/snapshot/`).

        Validates before launching anything: SandboxError when no usable
        snapshot exists, so callers just call it and fall back on the error
        (the worker: `start()` for the base, the base for a session snapshot).
        Restoring a SESSION snapshot consumes it: the running guest diverges
        from it, so `has_snapshot(snapshot_dir)` is False afterwards (both
        backends). The base snapshot is never consumed."""
        raise SandboxError(f"{type(self).__name__} has no snapshots")

    def snapshot(self, snapshot_dir: Path) -> None:
        """Pause the running guest and write a full snapshot (vmstate + memory
        + a copy-on-write-safe reference to workspace.img) into `snapshot_dir`,
        then stop the VM. Called only after the guest emitted `parked` (or is
        idle at the base snapshot point)."""
        raise SandboxError(f"{type(self).__name__} has no snapshots")

    def has_snapshot(self, snapshot_dir: Path | None = None) -> bool:
        """True when restore(…, snapshot_dir) would find a usable snapshot."""
        return False
