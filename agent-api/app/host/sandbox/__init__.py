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
