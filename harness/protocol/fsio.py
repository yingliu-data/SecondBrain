"""fsio — atomic file writes and file digests. Stdlib only.

  atomic_write(path, data: bytes, *, durable=True, mode=None)
      write `data` to a temporary file in path's directory (mkstemp), fsync
      it, rename it over `path`; with durable=True the directory is fsynced
      too, so the rename itself survives a crash. A reader sees the old file
      or the new one, never a part. durable=False skips both fsyncs (the
      rename stays atomic): for files whose loss on a power cut is harmless.
      The file is created 0600 (mkstemp); `mode` (e.g. 0o644) sets another.
  atomic_writer(path, *, durable=True, mode=None)
      the same as a context manager yielding the open binary file, for
      callers that stream (the bulk server hashes while it writes). An
      exception inside the block removes the temporary file and leaves
      `path` untouched.
  file_sha256(path) -> str | None
      sha256 hex of a file's bytes (read in 64 KiB chunks); None when it
      cannot be read.

The parent directory must exist (callers create it: they know the mode).
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

CHUNK = 1 << 16


def fsync_dir(directory) -> None:
    fd = os.open(os.fspath(directory), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_writer(path, *, durable: bool = True, mode: int | None = None):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            yield fh
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
        os.replace(tmp, path)       # replaces a symlink itself, never its target
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if durable:
        fsync_dir(path.parent)


def atomic_write(path, data: bytes, *, durable: bool = True, mode: int | None = None) -> None:
    with atomic_writer(path, durable=durable, mode=mode) as fh:
        fh.write(data)


def file_sha256(path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()
