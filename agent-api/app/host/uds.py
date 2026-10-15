"""uds — Unix sockets at any path length, the host's side.

sun_path holds 108 bytes on Linux; a session directory deep under
data/sessions can push `{session_dir}/ipc/v.sock_5010` or
`{session_dir}/vm/firecracker.sock` past it. The connect half is the
stdlib-only protocol.uds (the guest's test connector uses it too); this
module adds the listening half and the path form Firecracker is handed.

  fits_sun_path(path)          the absolute form fits (UNIX_PATH_MAX)
  connect_unix(path, timeout)  connect() at any length (/proc/self/fd when long)
  bind_unix(sock, path)        bind() at any length (/proc/self/fd when long)
  firecracker_path(path, cwd)  absolute when it fits, else relative to `cwd`
                               (the Firecracker process's working directory)

Standard library and protocol only: sandbox/firecracker.py imports it and
harness/image/build_snapshot.py runs that without the agent-api dependencies.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

from protocol.uds import UNIX_PATH_MAX, connect_unix, fits_sun_path, via_dirfd

__all__ = ["UNIX_PATH_MAX", "bind_unix", "connect_unix", "firecracker_path", "fits_sun_path"]


def bind_unix(sock: socket.socket, path) -> None:
    absolute = os.fspath(path)
    if fits_sun_path(absolute):
        sock.bind(absolute)
    else:
        via_dirfd(absolute, sock.bind)


def firecracker_path(path: Path, cwd: Path) -> str:
    """The form of a socket path Firecracker receives: absolute when it fits
    sun_path, else relative to the Firecracker process's cwd."""
    absolute = str(path)
    if fits_sun_path(absolute):
        return absolute
    return os.path.relpath(absolute, str(cwd))
