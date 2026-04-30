"""Per-session serialization + global concurrency cap.

Two concurrent POSTs to the same ``session_id`` must serialize — they both
read/write the same ``history.jsonl`` and can race the agent loop. Two
different sessions run in parallel up to the global semaphore limit.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class SessionQueue:
    def __init__(self, max_concurrent: int = 5):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def run(self, key: str):
        async with self._sem:
            lock = self._lock_for(key)
            async with lock:
                try:
                    yield
                finally:
                    if not lock.locked() and key in self._locks:
                        pass
