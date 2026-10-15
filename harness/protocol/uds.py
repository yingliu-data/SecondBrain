"""uds — Unix-socket connect at any path length. Stdlib only.

sun_path holds 108 bytes on Linux; a session directory deep under
data/sessions can push `{session_dir}/ipc/v.sock_5010` or
`{session_dir}/vm/firecracker.sock` past it. When the absolute path does not
fit, connect_unix goes through `/proc/self/fd/{dirfd}/{name}` (a directory fd
opened with O_PATH), which always fits.

  UNIX_PATH_MAX             100: the limit used (room under 108 for
                            Firecracker's own "_PORT" suffixes)
  fits_sun_path(path)       the absolute form fits
  connect_unix(path, timeout)  a connected AF_UNIX stream socket
  shutdown_close(sock)      shutdown(SHUT_RDWR) then close, OSError ignored:
                            wakes any thread blocked on the socket (any
                            family: vsock too)

The host's bind half is agent-api's app/host/uds.py; the guest's test
connector (guest.transport.UnixConnector) uses connect_unix.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

UNIX_PATH_MAX = 100


def fits_sun_path(path) -> bool:
    return len(os.fsencode(os.fspath(path))) <= UNIX_PATH_MAX


def shutdown_close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def via_dirfd(path, fn):
    """fn(short_path) with `path` reachable as /proc/self/fd/{dirfd}/{name}
    (the directory held open for the call)."""
    path = Path(path)
    dirfd = os.open(str(path.parent), os.O_PATH | os.O_DIRECTORY)
    try:
        return fn(f"/proc/self/fd/{dirfd}/{path.name}")
    finally:
        os.close(dirfd)


def connect_unix(path, timeout: float | None = None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        absolute = os.fspath(path)
        if fits_sun_path(absolute):
            sock.connect(absolute)
        else:
            via_dirfd(absolute, sock.connect)
    except BaseException:
        sock.close()
        raise
    return sock
