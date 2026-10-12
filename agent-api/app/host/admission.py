"""admission — the global cap on running tasks, shared by both drivers.

  Admission(max_concurrent)   the process-wide semaphore (Workers owns one;
                              HARNESS_MAX_CONCURRENT_TASKS); None = no cap
  admission.slot()            a Slot: one Worker's share of it

A Worker holds its Slot while it runs a task: taken before the task (or the
wake of a task parked on a question) is sent to the guest, given back when
the task parks on a question (a wait of up to answer_wait_s, not work) and
when the task ends. Tasks of one session are serialised by the Worker's
own task lock, taken first: a task queued behind its own session holds no
slot.
"""
from __future__ import annotations

import asyncio


class Slot:
    """One holder's share of an Admission. acquire() is idempotent (a held
    slot is not taken twice, also by concurrent callers), release() too."""

    def __init__(self, admission: "Admission") -> None:
        self._admission = admission
        self._lock = asyncio.Lock()
        self.held = False

    async def acquire(self) -> None:
        sem = self._admission._sem
        if self.held or sem is None:
            self.held = True
            return
        async with self._lock:
            if not self.held:
                await sem.acquire()
                self.held = True
                self._admission.in_use += 1

    def release(self) -> None:
        if self.held:
            self.held = False
            if self._admission._sem is not None:
                self._admission.in_use -= 1
                self._admission._sem.release()


class Admission:
    def __init__(self, max_concurrent: int | None) -> None:
        self.max_concurrent = max_concurrent
        self._sem = (asyncio.Semaphore(max_concurrent)
                     if max_concurrent is not None and max_concurrent > 0 else None)
        self.in_use = 0             # slots held (counted by Slot; capped only)

    def slot(self) -> Slot:
        return Slot(self)

    @property
    def free(self) -> int | None:
        """Slots not held (None: no cap)."""
        return None if self._sem is None else self.max_concurrent - self.in_use
