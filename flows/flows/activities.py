"""Payloads and activity signatures for the session workflow (PLAN section 10).

The dataclasses here are the contract between ``flows.session_workflow`` and
the host half of agent-api (``app/host/worker.py``), which will provide the real
bodies in M5.  The bodies below are STUBS: they record every call in
``CALLS``, sleep briefly and return the outcome scripted in ``SCRIPTS`` (or
``done``), so the workflow logic can be tested on the SDK test server without
a VM.

``run_task`` is the one activity that heartbeats.  Heartbeat details carry the
last acked ``seq`` of the guest event log; on a retry the new attempt reads
``activity.info().heartbeat_details`` and resumes from that seq instead of
re-running a model turn (PLAN 4.5).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from temporalio import activity
from temporalio.exceptions import ApplicationError

from flows import TASK_QUEUE

Outcome = Literal["done", "failed", "timeout", "escalated", "parked", "aborted"]
"""The one outcome vocabulary: what ``run_task`` returns, what ``deliver`` gets."""


# --------------------------------------------------------------------------- #
# Payloads (shared by workflow, activities and client; dataclasses only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionRef:
    """Identity of one session; ``workflow_id()`` is derived from it."""

    user: str
    tenant: str
    session_id: str


@dataclass
class TaskSignal:
    """``task`` signal: one input.  Redelivered POSTs resend the same input_id."""

    input_id: str
    text: str
    kind: str = "task"
    """``task`` (chat) or ``schedule`` (Temporal Schedule fire)."""
    wall_clock_s: int | None = None
    """Overrides ``SessionConfig.wall_clock_s`` for this task."""


@dataclass
class ActivityOptions:
    timeout_s: int
    """``start_to_close_timeout``."""
    max_attempts: int = 1
    heartbeat_s: int | None = None
    """``heartbeat_timeout``; only ``run_task`` heartbeats."""
    retry_initial_s: float = 1.0


def _default_activity_options() -> dict[str, ActivityOptions]:
    return {
        "prepare_vm": ActivityOptions(timeout_s=60, max_attempts=3),
        "run_task": ActivityOptions(timeout_s=600, max_attempts=2, heartbeat_s=120),
        "park_vm": ActivityOptions(timeout_s=60, max_attempts=3),
        "deliver": ActivityOptions(timeout_s=60, max_attempts=3),
        "signal_session": ActivityOptions(timeout_s=30, max_attempts=5),
    }


@dataclass
class SessionConfig:
    """Per-tenant knobs of a session workflow (PLAN 10: ``wall_clock_s``,
    ``answer_wait_s``, ``max_tasks_per_run`` come from the tenant entry)."""

    max_tasks_per_run: int = 50
    """Tasks per workflow run before continue-as-new."""
    answer_wait_s: int = 24 * 3600
    """How long a parked task waits for an ``answer`` (phone 24 h, guest 60 s)."""
    tick_timeout_s: int = 600
    """Execution timeout of ``ScheduleTickWorkflow`` (a schedule fire)."""
    activities: dict[str, ActivityOptions] = field(default_factory=_default_activity_options)
    """Timeouts and retries per activity name; ``run_task.timeout_s`` is the
    default wall clock of a task (``TaskSignal.wall_clock_s`` overrides it)."""

    @property
    def wall_clock_s(self) -> int:
        return self.activities["run_task"].timeout_s

    @classmethod
    def from_tenant(cls, tenant_entry: dict[str, Any]) -> SessionConfig:
        """Config from a ``tenants.json`` entry; missing keys keep the defaults."""
        cfg = cls()
        if "wall_clock_s" in tenant_entry:
            cfg.activities["run_task"] = replace(
                cfg.activities["run_task"], timeout_s=int(tenant_entry["wall_clock_s"])
            )
        if "answer_wait_s" in tenant_entry:
            cfg.answer_wait_s = int(tenant_entry["answer_wait_s"])
        if "max_tasks_per_run" in tenant_entry:
            cfg.max_tasks_per_run = int(tenant_entry["max_tasks_per_run"])
        return cfg


@dataclass
class PrepareVM:
    session: SessionRef
    input_id: str
    resume_from_park: bool = False
    """True when restoring a parked snapshot after an ``answer`` signal."""


@dataclass
class RunTask:
    session: SessionRef
    input_id: str
    text: str
    kind: str = "task"
    """``task`` for a chat input, ``schedule`` for a Temporal Schedule fire."""
    resume: bool = False
    """True when continuing a task after a parked question was answered."""
    answer: str | None = None
    question_id: str | None = None
    wall_clock_s: int = 600


@dataclass
class RunResult:
    outcome: Outcome
    question_id: str | None = None
    """Set when ``parked`` (the question asked) and echoed on the resume."""
    last_seq: int = 0


@dataclass
class ParkVM:
    session: SessionRef
    input_id: str
    question_id: str


@dataclass
class Deliver:
    session: SessionRef
    input_id: str
    outcome: Outcome
    question_id: str | None = None


@dataclass
class SignalSession:
    """Input of the glue activity behind a Temporal Schedule fire.

    ``task.input_id`` is filled in by the activity from the tick workflow's id
    (``{schedule-id}-{fire-time}``), so a retry cannot double-run the task.
    """

    session: SessionRef
    task: TaskSignal
    task_queue: str = TASK_QUEUE
    config: SessionConfig = field(default_factory=SessionConfig)
    """Used when the fire has to start the session workflow."""


# --------------------------------------------------------------------------- #
# Stub bodies
# --------------------------------------------------------------------------- #

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
    # attempt.  The real body resumes the guest from this seq (PLAN 4.5).
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
