"""SessionWorkflow: one long-lived Temporal workflow per session (PLAN D11, section 10).

Deterministic workflow code.  Imports only the Temporal SDK, the stdlib and
``flows.activities`` (payload dataclasses).  Nothing here touches the VM, the
session directory or the phone; that is what the activities are for.

Workflow id: ``{user}/{tenant}/{session_id}`` (``workflow_id()``).

Loop (per task signal, deduped by ``input_id``):

    prepare_vm  -> run_task -> [parked: park_vm -> wait answer | timer] -> deliver

``run_task`` is retried once; the retry attempt reads the previous attempt's
last heartbeat details (``activity.info().heartbeat_details``) and resumes the
guest from that seq.  The workflow never sees the heartbeat details itself:
they live on the activity task on the server and are handed to the next
attempt by the server.  The workflow only sees the final ``RunResult``.

Continue-as-new after ``config.max_tasks_per_run`` tasks or once the history
passes ``config.max_history_events`` (checked between tasks and while a task is
parked), carrying the seen input ids, the queued-but-unrun tasks and the task
in flight if parked, with its answer deadline (a hop does not re-arm the wait).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from flows.activities import (
        ActivityOptions,
        Deliver,
        ParkVM,
        PrepareVM,
        RunResult,
        RunTask,
        SessionConfig,
        SessionRef,
        SignalSession,
        TaskSignal,
    )

__all__ = [
    "AbortSignal", "AnswerSignal", "ScheduleTickWorkflow", "SessionConfig",
    "SessionState", "SessionWorkflow", "Status", "TaskSignal", "WORKFLOWS",
    "parse_workflow_id", "workflow_id",
]

SEEN_LIMIT = 2000
"""Most recent input ids kept for dedupe across continue-as-new."""


# --------------------------------------------------------------------------- #
# Ids
# --------------------------------------------------------------------------- #


def workflow_id(user: str, tenant: str, session_id: str) -> str:
    """``{user}/{tenant}/{session_id}`` — the id of a session's workflow."""
    return f"{user}/{tenant}/{session_id}"


def parse_workflow_id(wid: str) -> SessionRef:
    user, tenant, session_id = wid.split("/", 2)
    return SessionRef(user=user, tenant=tenant, session_id=session_id)


# --------------------------------------------------------------------------- #
# Signal / query / start payloads
# --------------------------------------------------------------------------- #


@dataclass
class AnswerSignal:
    """``answer`` signal: ``POST /chat {answer_to}`` for a parked question."""

    question_id: str
    text: str


@dataclass
class AbortSignal:
    reason: str


@dataclass
class SessionState:
    """Start argument of ``SessionWorkflow``; also what continue-as-new carries."""

    session: SessionRef
    config: SessionConfig = field(default_factory=SessionConfig)
    seen: list[str] = field(default_factory=list)
    pending: list[TaskSignal] = field(default_factory=list)
    in_flight: tuple[TaskSignal, RunResult] | None = None
    """A task that was parked when the previous run ended: it is resumed from
    that ``RunResult`` (``park_vm`` again, then wait for the answer)."""
    in_flight_deadline: float | None = None
    """Answer deadline of ``in_flight`` (epoch seconds, workflow time); None
    re-arms a full ``answer_wait_s`` (a state written before M5)."""
    generation: int = 0
    """Number of continue-as-new hops so far."""


@dataclass
class Status:
    state: str
    """``idle`` | ``running`` | ``parked``."""
    current_input_id: str | None
    question_id: str | None
    seen: int
    pending: int
    tasks_run: int
    generation: int


def _activity_kwargs(opts: ActivityOptions, *, timeout_s: int | None = None) -> dict[str, Any]:
    """``execute_activity``/``start_activity`` options from a config row."""
    kwargs: dict[str, Any] = {
        "start_to_close_timeout": timedelta(seconds=timeout_s or opts.timeout_s),
        "retry_policy": RetryPolicy(
            maximum_attempts=opts.max_attempts,
            initial_interval=timedelta(seconds=opts.retry_initial_s),
        ),
    }
    if opts.heartbeat_s is not None:
        kwargs["heartbeat_timeout"] = timedelta(seconds=opts.heartbeat_s)
    if timeout_s:
        # The task's wall clock bounds all attempts together: a retry of a
        # timed-out attempt would otherwise get a second full wall clock.
        kwargs["schedule_to_close_timeout"] = timedelta(seconds=timeout_s)
    return kwargs


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


@dataclass
class _Live:
    """Signal state of the task in flight; created and discarded by ``_run_one``."""

    question_id: str | None = None
    """The parked question an ``answer`` must name."""
    answer: AnswerSignal | None = None
    abort: AbortSignal | None = None
    deadline: float | None = None
    """Epoch seconds (workflow time) when the parked question escalates."""

    def take(self, name: str) -> Any:
        """Consume ``answer``/``abort`` once."""
        value = getattr(self, name)
        setattr(self, name, None)
        return value


@workflow.defn(name="SessionWorkflow")
class SessionWorkflow:
    def __init__(self) -> None:
        self._state: SessionState | None = None
        self._cfg = SessionConfig()
        self._seen: dict[str, None] = {}
        """Insertion-ordered; the oldest id is evicted past ``SEEN_LIMIT``."""
        self._pending: deque[TaskSignal] = deque()
        self._live: _Live | None = None
        self._phase = "idle"
        self._current: TaskSignal | None = None
        self._tasks_run = 0
        self._signals = 0
        """Signals received by this run (any, ignored ones too): they are what
        grows the history of a parked run."""

    # ---- signals ----------------------------------------------------------

    @workflow.signal(name="task")
    def task(self, sig: TaskSignal) -> None:
        self._signals += 1
        if sig.input_id in self._seen:
            workflow.logger.info("duplicate task ignored: %s", sig.input_id)
            return
        self._remember(sig.input_id)
        self._pending.append(sig)

    @workflow.signal(name="answer")
    def answer(self, sig: AnswerSignal) -> None:
        self._signals += 1
        parked = self._live.question_id if self._live else None
        if parked is None or sig.question_id != parked:
            workflow.logger.info("answer for question %r ignored (parked on %r)",
                                 sig.question_id, parked)
            return
        self._live.answer = sig

    @workflow.signal(name="abort")
    def abort(self, sig: AbortSignal) -> None:
        self._signals += 1
        if self._live is None:
            workflow.logger.info("abort while idle ignored: %s", sig.reason)
            return
        self._live.abort = sig

    # ---- query ------------------------------------------------------------

    @workflow.query(name="status")
    def status(self) -> Status:
        return Status(
            state=self._phase,
            current_input_id=self._current.input_id if self._current else None,
            question_id=self._live.question_id if self._live else None,
            seen=len(self._seen),
            pending=len(self._pending),
            tasks_run=self._tasks_run,
            generation=self._state.generation if self._state else 0,
        )

    # ---- main loop --------------------------------------------------------

    @workflow.run
    async def run(self, state: SessionState) -> None:
        self._state = state
        self._cfg = state.config
        # Signals delivered before run() started are already in _pending and
        # were deduped against an empty seen set: re-filter them against the
        # carried one, then put the carried queue in front.
        carried_seen = set(state.seen)
        early = [t for t in self._pending if t.input_id not in carried_seen]
        self._seen = {}
        for iid in [*state.seen, *(t.input_id for t in early)]:
            self._remember(iid)
        self._pending = deque([*state.pending, *early])

        if state.in_flight is not None:
            await self._run_one(*state.in_flight, deadline=state.in_flight_deadline)

        while True:
            if self._tasks_run >= self._cfg.max_tasks_per_run or (
                    self._tasks_run > 0 and self._history_full()):
                await self._continue_as_new()
            await workflow.wait_condition(lambda: bool(self._pending))
            await self._run_one(self._pending.popleft())

    # ---- one task ---------------------------------------------------------

    async def _run_one(self, task: TaskSignal, carried: RunResult | None = None,
                       deadline: float | None = None) -> None:
        """Run one task start to finish; ``carried`` is the parked result of a
        task handed over by continue-as-new, which skips the first run, and
        ``deadline`` its carried answer deadline."""
        self._current = task
        live = self._live = _Live()
        try:
            try:
                result = carried or await self._start(task)
                while result.outcome == "parked":
                    qid = result.question_id or ""
                    # The answer may arrive while the VM is still being parked:
                    # the signal handler must already know the question.
                    live.question_id = qid
                    live.deadline = deadline if deadline is not None else (
                        workflow.now().timestamp() + self._cfg.answer_wait_s)
                    deadline = None  # a later question in this task gets a full wait
                    self._phase = "parked"
                    # Idempotent: re-called for a carried parked task.
                    await self._activity("park_vm", ParkVM(
                        session=self.session, input_id=task.input_id, question_id=qid))
                    result = await self._wait_answer_and_resume(task, qid)
            except ActivityError as err:
                # prepare_vm/park_vm out of retries: this task failed; the
                # workflow stays signalable for the next one (PLAN 10).
                workflow.logger.warning("task %s failed: %s", task.input_id, err)
                result = RunResult(outcome="failed", question_id=live.question_id)
            try:
                await self._activity("deliver", Deliver(
                    session=self.session, input_id=task.input_id,
                    outcome=result.outcome, question_id=result.question_id))
            except ActivityError as err:
                workflow.logger.error("deliver %s failed: %s", task.input_id, err)
        finally:
            self._tasks_run += 1
            self._live = None
            self._current = None
            self._phase = "idle"

    async def _start(self, task: TaskSignal) -> RunResult:
        self._phase = "running"
        await self._activity("prepare_vm", PrepareVM(
            session=self.session, input_id=task.input_id, resume_from_park=False))
        return await self._run_task(task, resume=False, answer=None, question_id=None)

    async def _run_task(self, task: TaskSignal, *, resume: bool, answer: str | None,
                        question_id: str | None) -> RunResult:
        """Run the heartbeating activity; one retry with heartbeat details.

        The retry is the server's: on attempt 1 failing (worker crash,
        heartbeat timeout, raised error) the server schedules attempt 2 and
        attaches the last recorded heartbeat details to it.  The workflow is
        not involved in that hand-off and cannot read the details; it just
        awaits the activity's final result.
        """
        live = self._live
        assert live is not None
        inp = RunTask(
            session=self.session,
            input_id=task.input_id,
            text=task.text,
            kind=task.kind,
            resume=resume,
            answer=answer,
            question_id=question_id,
            wall_clock_s=task.wall_clock_s or self._cfg.wall_clock_s,
        )
        handle = workflow.start_activity(
            "run_task", inp, result_type=RunResult,
            **_activity_kwargs(self._cfg.activities["run_task"], timeout_s=inp.wall_clock_s),
        )
        await workflow.wait_condition(lambda: handle.done() or live.abort is not None)
        if not handle.done():
            live.take("abort")
            handle.cancel()
            try:
                await handle
            except (ActivityError, asyncio.CancelledError):
                pass
            return RunResult(outcome="aborted", question_id=question_id)
        try:
            return await handle
        except ActivityError as err:
            outcome = "timeout" if isinstance(err.cause, TemporalTimeoutError) else "failed"
            workflow.logger.warning("run_task %s: %s", outcome, err)
            return RunResult(outcome=outcome, question_id=question_id)

    async def _wait_answer_and_resume(self, task: TaskSignal, question_id: str) -> RunResult:
        """Parked: wait for ``answer``, ``abort`` or the deadline timer.

        A run whose history passes ``max_history_events`` while parked
        continues-as-new from here, carrying the task and its deadline.  It
        only does so after this run made progress or received a signal, so a
        threshold below a fresh run's size cannot loop.
        """
        live = self._live
        assert live is not None and live.deadline is not None

        def _woken() -> bool:
            return (live.answer is not None or live.abort is not None
                    or ((self._tasks_run > 0 or self._signals > 0) and self._history_full()))

        remaining = live.deadline - workflow.now().timestamp()
        if remaining <= 0 and not _woken():
            return RunResult(outcome="escalated", question_id=question_id)
        if remaining > 0:
            try:
                await workflow.wait_condition(_woken, timeout=timedelta(seconds=remaining))
            except asyncio.TimeoutError:
                return RunResult(outcome="escalated", question_id=question_id)
        if live.answer is None and live.abort is None:
            await self._continue_as_new()
        ans: AnswerSignal | None = live.take("answer")
        if ans is None:
            live.take("abort")
            return RunResult(outcome="aborted", question_id=question_id)
        live.question_id = None
        self._phase = "running"
        await self._activity("prepare_vm", PrepareVM(
            session=self.session, input_id=task.input_id, resume_from_park=True))
        return await self._run_task(task, resume=True, answer=ans.text,
                                    question_id=ans.question_id)

    # ---- helpers ----------------------------------------------------------

    @property
    def session(self) -> SessionRef:
        assert self._state is not None
        return self._state.session

    async def _activity(self, name: str, payload: Any) -> Any:
        """Execute activity ``name`` with the options of ``config.activities[name]``."""
        return await workflow.execute_activity(
            name, payload, **_activity_kwargs(self._cfg.activities[name]))

    def _history_full(self) -> bool:
        return workflow.info().get_current_history_length() >= self._cfg.max_history_events

    async def _continue_as_new(self) -> None:
        """Hop to a new run (raises ``ContinueAsNewError``; never returns)."""
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.logger.info("continue-as-new: %d tasks, %d history events, %s",
                             self._tasks_run, workflow.info().get_current_history_length(),
                             self._phase)
        workflow.continue_as_new(self._carry())

    def _remember(self, input_id: str) -> None:
        self._seen[input_id] = None
        if len(self._seen) > SEEN_LIMIT:
            del self._seen[next(iter(self._seen))]

    def _carry(self) -> SessionState:
        assert self._state is not None
        in_flight = None
        deadline = None
        if self._phase == "parked":
            assert self._current is not None and self._live is not None
            in_flight = (self._current, RunResult(outcome="parked", question_id=self._live.question_id))
            deadline = self._live.deadline
        return replace(
            self._state,
            seen=list(self._seen),
            pending=list(self._pending),
            in_flight=in_flight,
            in_flight_deadline=deadline,
            generation=self._state.generation + 1,
        )


# --------------------------------------------------------------------------- #
# Schedule fire -> signal-with-start of the session workflow
# --------------------------------------------------------------------------- #


@workflow.defn(name="ScheduleTickWorkflow")
class ScheduleTickWorkflow:
    """Started by a Temporal Schedule; signals-with-starts the session workflow.

    A Schedule action can only *start* a workflow (no signal-with-start, no
    id-conflict policy in the SDK 1.34 action), and the session workflow is
    usually already running, so the schedule starts this short workflow whose
    only job is the ``signal_session`` activity (``flows.schedules``).  The
    activity takes this workflow's id (``{schedule-id}-{fire-time}``) as the
    task's input_id, so a retry of the tick or of the activity cannot
    double-run the task.
    """

    @workflow.run
    async def run(self, inp: SignalSession) -> str:
        return await workflow.execute_activity(
            "signal_session", inp, result_type=str,
            **_activity_kwargs(inp.config.activities["signal_session"]),
        )


WORKFLOWS = [SessionWorkflow, ScheduleTickWorkflow]
