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
        self._refs: dict[str, int] = {}

    @asynccontextmanager
    async def run(self, key: str):
        async with self._sem:
            # Refcount the lock: evict only when no holder AND no waiter
            # references the key, so late arrivals always share the same Lock.
            self._refs[key] = self._refs.get(key, 0) + 1
            lock = self._locks.setdefault(key, asyncio.Lock())
            try:
                async with lock:
                    yield
            finally:
                self._refs[key] -= 1
                if self._refs[key] <= 0:
                    self._refs.pop(key, None)
                    self._locks.pop(key, None)
