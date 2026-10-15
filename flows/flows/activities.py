"""Payloads and activity signatures for the session workflow.

The dataclasses here are the contract between ``flows.session_workflow`` and
the host half of agent-api (``app/host/activities.py``), which provides the
real bodies of the four session activities (``prepare_vm``, ``run_task``,
``park_vm``, ``deliver``; the workflow calls them by name).  Stub bodies for
the workflow tests and the standalone dev worker live in
``flows/tests/stubs.py``.

``run_task`` is the one activity that heartbeats.  Heartbeat details carry the
last acked ``seq`` of the guest event log; on a retry the new attempt reads
``activity.info().heartbeat_details`` and resumes from that seq instead of
re-running a model turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

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
    """Overrides ``SessionConfig.wall_clock_s`` for this task (agent-api sets it
    from the tenant's current config on every chat input)."""


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


WALL_CLOCK_GRACE_S = 30
"""What the host waits past a task's wall clock, counted from when the task
took its slot of the task cap (the guest settles ``timeout`` at the wall
clock itself; this covers boot and the settle's way back): the host's
backstop on both paths.  ``run_task``'s Temporal timeouts add
``SessionConfig.max_queue_wait_s`` on top (``run_task_timeout_s``)."""

MAX_QUEUE_WAIT_S = 3600
"""Default ``SessionConfig.max_queue_wait_s``."""


def run_task_timeout_s(wall_clock_s: int, max_queue_wait_s: int) -> int:
    """``run_task``'s start-to-close and schedule-to-close timeout: a safety
    net only.  The task's time limit counts from its start on the host (the
    guest's wall clock, the host's backstop of it + ``WALL_CLOCK_GRACE_S``);
    the time before it - Temporal's task queue, the wait for a slot of the
    task cap - is bounded by ``max_queue_wait_s``, liveness by the heartbeat
    (which ticks while the task is queued too).  A cancel before the task
    started drops it on the host: it never runs later."""
    return wall_clock_s + WALL_CLOCK_GRACE_S + max_queue_wait_s


SESSION_CONFIG_KEYS = ("wall_clock_s", "answer_wait_s", "max_tasks_per_run",
                       "max_history_events")
"""The ``tenants.json`` keys ``SessionConfig.from_tenant`` reads (agent-api's
``Tenant`` declares the same fields and passes them through)."""


@dataclass
class SessionConfig:
    """Per-tenant knobs of a session workflow (``wall_clock_s``,
    ``answer_wait_s``, ``max_tasks_per_run`` come from the tenant entry)."""

    max_tasks_per_run: int = 50
    """Tasks per workflow run before continue-as-new."""
    answer_wait_s: int = 24 * 3600
    """How long a parked task waits for an ``answer`` (phone 24 h, guest 60 s)."""
    tick_timeout_s: int = 600
    """Execution timeout of ``ScheduleTickWorkflow`` (a schedule fire)."""
    max_history_events: int = 10_000
    """History length past which a run continues-as-new (between tasks, or
    while a task is parked); the other trigger next to ``max_tasks_per_run``."""
    max_queue_wait_s: int = MAX_QUEUE_WAIT_S
    """How long a task may wait to start (Temporal's task queue, a slot of
    agent-api's task cap) before ``run_task`` times out; added to its
    Temporal timeouts only (``run_task_timeout_s``), never to the task's own
    wall clock.  agent-api: ``HARNESS_MAX_QUEUE_WAIT_S``."""
    activities: dict[str, ActivityOptions] = field(default_factory=_default_activity_options)
    """Timeouts and retries per activity name; ``run_task.timeout_s`` is the
    default wall clock of a task (``TaskSignal.wall_clock_s`` overrides it)."""

    @property
    def wall_clock_s(self) -> int:
        return self.activities["run_task"].timeout_s

    @classmethod
    def from_tenant(cls, tenant_entry: dict[str, Any]) -> SessionConfig:
        """Config from a ``tenants.json`` entry; missing (or null) keys keep the
        defaults.  Reads the ``SESSION_CONFIG_KEYS``: each an int >= 1, but
        ``answer_wait_s`` 0 = the guest never parks on a question (deny path;
        a park that happens anyway escalates at once)."""
        cfg = cls()
        for key in SESSION_CONFIG_KEYS:
            value = tenant_entry.get(key)
            if value is None:
                continue
            n, minimum = int(value), 0 if key == "answer_wait_s" else 1
            if n < minimum:
                raise ValueError(
                    f"tenant {tenant_entry.get('name')!r}: {key} must be >= {minimum}, got {n}")
            if key == "wall_clock_s":
                cfg.activities["run_task"] = replace(cfg.activities["run_task"], timeout_s=n)
            else:
                setattr(cfg, key, n)
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
    """The task's wall clock: the guest gets it in the ``task`` message and
    settles ``timeout`` at it, counted from the task's start on the host;
    the activity's Temporal timeout is ``run_task_timeout_s``."""


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
