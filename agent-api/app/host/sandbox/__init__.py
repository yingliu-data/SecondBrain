"""sandbox — where the guest harness runs (PLAN.md D6a, D18).

`Sandbox` is the interface the worker drives. Two backends:

  firecracker.py  FirecrackerSandbox: a microVM per session; vsock uds_path
                  = {session_dir}/ipc/v.sock, so guest-initiated connections
                  to port P land on {session_dir}/ipc/v.sock_P (Endpoints)
  local.py        LocalSandbox: the guest as a subprocess connecting to the
                  same Unix sockets (--connect unix:{session_dir}/ipc);
                  tests and the no-KVM build machine
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Sandbox(ABC):
    """One guest runtime. `start` boots the guest for a session directory;
    the guest then connects out to the session's endpoints. `stop` kills it.
    A sandbox instance belongs to one session and may be started again after
    a stop (a new guest run)."""

    @abstractmethod
    def start(self, session_dir: Path) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_alive(self) -> bool: ...

    # --- M3: snapshots (PLAN.md D18, docs/PROTOCOL.md §5–6) -----------------
    # Default implementations raise; LocalSandbox emulates them (a parked
    # "snapshot" is the guest process exiting after `parked`; restore starts
    # a fresh guest process that reconnects with resume semantics).

    def restore(self, session_dir: Path, snapshot_dir: Path | None = None) -> None:
        """Start the guest for `session_dir` from a snapshot instead of a cold
        boot. `snapshot_dir=None` → the shared BASE snapshot (harness idle
        before connecting out); otherwise a per-session snapshot previously
        written by `snapshot()` (normally `{session_dir}/vm/snapshot/`).
        Raises SandboxError when no usable snapshot exists; the worker then
        falls back to `start()` (rootfs boot)."""
        raise NotImplementedError

    def snapshot(self, snapshot_dir: Path) -> None:
        """Pause the running guest and write a full snapshot (vmstate + memory
        + a copy-on-write-safe reference to workspace.img) into `snapshot_dir`,
        then stop the VM. Called only after the guest emitted `parked` (or is
        idle at the base snapshot point)."""
        raise NotImplementedError

    def has_snapshot(self, snapshot_dir: Path | None = None) -> bool:
        """True when restore(…, snapshot_dir) would find a usable snapshot."""
        return False
