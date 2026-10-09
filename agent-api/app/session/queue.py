"""Per-session serialization + global concurrency cap.

Two concurrent POSTs to the same ``session_id`` must serialize — they both
read/write the same ``history.jsonl`` and can race the agent loop. Two
different sessions run in parallel up to the global semaphore limit.

The session lock is taken first: a turn queued behind its own session holds
no global slot. The holder gets its `Slot`, which it may give back while it
waits on something that is not work (a question park, chat.py) and take
again before it goes on; the session lock stays held throughout.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class Slot:
    """One holder's share of the global cap."""

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self.held = False

    async def acquire(self) -> None:
        if not self.held:
            await self._sem.acquire()
            self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self._sem.release()


class SessionQueue:
    def __init__(self, max_concurrent: int = 5):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @asynccontextmanager
    async def run(self, key: str):
        # Refcount the lock: evict only when no holder AND no waiter
        # references the key, so late arrivals always share the same Lock.
        self._refs[key] = self._refs.get(key, 0) + 1
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                slot = Slot(self._sem)
                await slot.acquire()
                try:
                    yield slot
                finally:
                    slot.release()
        finally:
            self._refs[key] -= 1
            if self._refs[key] <= 0:
                self._refs.pop(key, None)
                self._locks.pop(key, None)
