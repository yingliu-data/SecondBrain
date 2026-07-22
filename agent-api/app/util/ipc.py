"""Atomic-rename filesystem IPC helpers.

Writers create ``{path}.tmp`` and ``os.replace`` it onto the final name.
On POSIX same-filesystem rename is atomic; readers never see a torn file.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path | str, payload: Any) -> None:
    """Write ``payload`` as JSON to ``path`` atomically via tmp + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json_once(path: Path | str, *, delete: bool = True) -> Any | None:
    """Read JSON from ``path``; if ``delete``, remove the file after read.

    Returns ``None`` if the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return None
    if delete:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return data


async def await_ipc_file(
    path: Path | str,
    timeout: float,
    *,
    in_memory_event: asyncio.Event | None = None,
    poll_interval: float = 0.1,
) -> bool:
    """Wait for ``path`` to appear, up to ``timeout`` seconds.

    If ``in_memory_event`` is provided, wait on it (fast path for same-process
    writers). Fall back to polling so a restart or cross-process writer still
    wakes us up. Returns ``True`` if the file exists, ``False`` on timeout.
    """
    path = Path(path)
    if path.exists():
        return True
    if in_memory_event is not None:
        try:
            await asyncio.wait_for(in_memory_event.wait(), timeout=timeout)
            return path.exists()
        except asyncio.TimeoutError:
            return path.exists()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if path.exists():
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval, remaining))
