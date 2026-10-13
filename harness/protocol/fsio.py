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
  atomic_write_json(path, obj, *, durable=True, mode=None, indent=None,
                    sort_keys=False, compact=False)
      atomic_write of json.dumps(obj) (UTF-8); indent adds a trailing
      newline, compact uses (",", ":") separators.
  swap_dir(staging, dest)
      replace directory `dest` (absent or not) with `staging` by rename: the
      old one moved to a `.{name}.old` sibling, then removed. A reader sees
      the old directory or the new one; a process holding an old file keeps
      its inode, nothing is truncated in place.
  file_sha256(path) -> str | None
      sha256 hex of a file's bytes (read in 64 KiB chunks); None when it
      cannot be read.

The parent directory must exist (callers create it: they know the mode).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
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


def atomic_write_json(path, obj, *, durable: bool = True, mode: int | None = None,
                      indent: int | None = None, sort_keys: bool = False,
                      compact: bool = False) -> None:
    text = json.dumps(obj, indent=indent, sort_keys=sort_keys,
                      separators=(",", ":") if compact else None)
    if indent is not None:
        text += "\n"
    atomic_write(path, text.encode("utf-8"), durable=durable, mode=mode)


def swap_dir(staging, dest) -> None:
    staging, dest = Path(staging), Path(dest)
    old = dest.parent / f".{dest.name}.old"
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    os.replace(staging, dest)
    shutil.rmtree(old, ignore_errors=True)


def file_sha256(path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()
