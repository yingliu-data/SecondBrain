"""Stub bodies of the four session activities (``prepare_vm``, ``run_task``,
``park_vm``, ``deliver``): the workflow tests and the standalone dev worker
(``python -m flows.worker``) register them; agent-api registers its real ones
(``app/host/activities.py``) under the same names and payloads.

They record every call in ``CALLS``, sleep briefly and return the outcome
scripted in ``SCRIPTS`` (or ``done``), so the workflow logic can be tested on
the SDK test server without a VM.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from flows.activities import Deliver, Outcome, ParkVM, PrepareVM, RunResult, RunTask


CALLS: list[dict[str, Any]] = []
"""Every stub call, in order: ``{"activity": name, "attempt": n, "input": ...,
"heartbeat_details": [...]}``.  Tests clear and inspect it."""


@dataclass
class Script:
    """What the ``run_task`` stub does for one input id (tests set ``SCRIPTS``)."""

    fail_once: bool = False
    """Raise on attempt 1 after a few heartbeats, so the retry can show that
    it received the heartbeat details."""
    park: bool = False
    """Return ``parked`` on the first (non-resume) run."""
    outcome: Outcome = "done"
    """Outcome to return when not parking."""


SCRIPTS: dict[str, Script] = {}
"""Per-``input_id`` script for the ``run_task`` stub; unlisted ids run ``Script()``.
The worker runs in the test process, so tests set this directly."""

STUB_SLEEP_S = 0.05
STUB_HEARTBEAT_S = 0.1
STUB_HEARTBEATS = 3
"""Heartbeats the ``run_task`` stub sends before finishing (or crashing)."""


def _record(name: str, inp: Any, **extra: Any) -> dict[str, Any]:
    info = activity.info()
    rec: dict[str, Any] = {
        "activity": name,
        "attempt": info.attempt,
        "input": inp,
        "heartbeat_details": list(info.heartbeat_details),
        "start_to_close_s": (info.start_to_close_timeout.total_seconds()
                             if info.start_to_close_timeout else None),
        "schedule_to_close_s": (info.schedule_to_close_timeout.total_seconds()
                                if info.schedule_to_close_timeout else None),
        "heartbeat_s": (info.heartbeat_timeout.total_seconds()
                        if info.heartbeat_timeout else None),
        **extra,
    }
    CALLS.append(rec)
    return rec


@activity.defn
async def prepare_vm(inp: PrepareVM) -> None:
    """Restore the base snapshot (or the parked one) and complete ``hello``."""
    _record("prepare_vm", inp)
    await asyncio.sleep(STUB_SLEEP_S)


@activity.defn
async def run_task(inp: RunTask) -> RunResult:
    """Drive one task through the guest; heartbeat the last acked seq."""
    info = activity.info()
    # Details of the LAST heartbeat the server accepted from the previous
    # attempt.  The real body resumes the guest from this seq.
    seq = int(info.heartbeat_details[0]) if info.heartbeat_details else 0
    rec = _record("run_task", inp)
    script = SCRIPTS.get(inp.input_id, Script())

    for _ in range(STUB_HEARTBEATS):
        seq += 1
        activity.heartbeat(seq)
        await asyncio.sleep(STUB_HEARTBEAT_S)

    if script.fail_once and info.attempt == 1:
        rec["crashed_at_seq"] = seq
        raise ApplicationError(f"simulated worker crash at seq {seq}")

    if script.park and not inp.resume:
        qid = inp.question_id or f"q-{inp.input_id}"
        return RunResult(outcome="parked", question_id=qid, last_seq=seq)
    return RunResult(outcome=script.outcome, last_seq=seq)


@activity.defn
async def park_vm(inp: ParkVM) -> None:
    """Snapshot the VM while the workflow waits for an answer.

    Must be idempotent: the workflow calls it once per parked round and again
    when a run resumes a carried parked task after continue-as-new, so the real
    body is a no-op when the snapshot for ``question_id`` already exists.
    """
    _record("park_vm", inp)
    await asyncio.sleep(STUB_SLEEP_S)


@activity.defn
async def deliver(inp: Deliver) -> None:
    """Workspace manifest, ticket projection, final SSE ``done``."""
    _record("deliver", inp)
    await asyncio.sleep(STUB_SLEEP_S)


STUB_ACTIVITIES = [prepare_vm, run_task, park_vm, deliver]
"""The four session activities, stub bodies.  agent-api registers its own
implementations with the same names and payloads."""
