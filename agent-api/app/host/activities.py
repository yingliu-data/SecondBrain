"""activities — the real bodies of the session workflow's activities (M5).

harness/docs/M5-CONTRACTS.md §2: same names and payloads as the stubs in
flows/activities.py; flows.worker.build_worker registers them (bound
methods of one HostActivities) next to flows' own `signal_session` glue.

  prepare_vm(PrepareVM)        Worker for the session; boot / wake the guest
  run_task(RunTask)->RunResult act() as a worker-owned task (a retry re-attaches
                               by input_id, never a second turn); heartbeat
                               {last_seq} at start and every heartbeat_s;
                               resume = ui_response, then wait; returns at the
                               settle or at the guest's question park;
                               cancelled (workflow abort) -> abort{aborted}
                               to the guest, wait for its settle, re-raise
  park_vm(ParkVM)              parked + snapshotted (normally already: the
                               worker adopted the guest's own park)
  deliver(Deliver)             escalated/aborted with no settle: wake + abort
                               so the guest writes it; the ticket projection
                               {session}/tickets/{ticket_id}.json; SSE error
                               (not done) + done to the input's streams; host
                               event workflow{delivered}. Idempotent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from flows.activities import (Deliver, ParkVM, PrepareVM, RunResult, RunTask,
                              SessionConfig, SessionRef)
from protocol.canonical import derive_id

from app.config import HARNESS_ABORT_WAIT_S, HARNESS_ACTIVITY_HEARTBEAT_S
from app.host.worker import Worker
from app.util.ipc import atomic_write_json

logger = logging.getLogger("host.activities")

SETTLE_OUTCOMES = ("done", "failed", "timeout", "escalated")


def answer_input_id(input_id: str, question_id: str) -> str:
    """The ui_response's input_id for a workflow `answer`: stable across
    retries, so the guest dedupes a resent answer."""
    return f"{input_id}:answer:{question_id}"


def ticket_id_for(ref: SessionRef, input_id: str, settled_seq: int | None) -> str:
    """derive_id(user, tenant, session_id, settle_seq, "ticket"); an input
    that never settled in the guest gets one derived from its input_id."""
    if settled_seq:
        return derive_id(ref.user, ref.tenant, ref.session_id, settled_seq, "ticket")
    return derive_id(ref.user, ref.tenant, f"{ref.session_id}/{input_id}", 1, "ticket")


def bind_workflow(w: Worker, tenant) -> SessionConfig:
    """The tenant's SessionConfig; puts `w` on the workflow path (hello
    limit answer_wait_s, deliver ends the streams) if it is not yet."""
    config = SessionConfig.from_tenant(tenant.workflow_entry())
    if w.answer_wait_s is None:
        w.answer_wait_s = config.answer_wait_s
    return config


def _floor_seq(details: Any) -> int:
    """The previous attempt's last heartbeat {last_seq} (a floor only: the
    SDK throttles heartbeats, M0 spike)."""
    try:
        d = details[0] if details else None
        return int(d.get("last_seq", 0)) if isinstance(d, dict) else int(d or 0)
    except (TypeError, ValueError, IndexError):
        return 0


class HostActivities:
    def __init__(self, workers, sessions, tenants, registry, *,
                 heartbeat_s: float = HARNESS_ACTIVITY_HEARTBEAT_S,
                 abort_wait_s: float = HARNESS_ABORT_WAIT_S) -> None:
        self.workers = workers          # app.host.worker.Workers
        self.sessions = sessions        # DirStore
        self.tenants = tenants          # TenantRegistry (get_by_name)
        self.registry = registry        # SkillRegistry
        self.heartbeat_s = heartbeat_s
        self.abort_wait_s = abort_wait_s

    def activities(self) -> list:
        return [self.prepare_vm, self.run_task, self.park_vm, self.deliver]

    # ------------------------------------------------------------ helpers
    def worker(self, ref: SessionRef) -> Worker:
        tenant = self.tenants.get_by_name(ref.tenant)
        if tenant is None or tenant.user != ref.user:
            raise ApplicationError(f"unknown tenant {ref.user}/{ref.tenant}", non_retryable=True)
        session = self.sessions.get_or_create(ref.user, ref.tenant, ref.session_id)
        w = self.workers.get_or_create(session, tenant, self.registry)
        bind_workflow(w, tenant)
        return w

    @staticmethod
    def _started(w: Worker, name: str, input_id: str, **extra) -> None:
        w.sync.host_event("workflow", {"kind": "activity_start", "activity": name,
                                       "input_id": input_id,
                                       "attempt": activity.info().attempt, **extra})

    async def _heartbeat(self, w: Worker) -> None:
        while True:
            activity.heartbeat({"last_seq": w.sync.last_seq})
            await asyncio.sleep(self.heartbeat_s)

    async def _abort_and_wait(self, w: Worker, input_id: str, reason: str) -> None:
        """abort{reason} to the guest, then wait (bounded) for that input's
        act() to finish: the guest writes the settle itself."""
        task = w.task_for(input_id)
        # abort{} is not input-scoped (the guest settles whatever it runs and
        # exits): only while this input's act() is the running one
        if task is None or task.done() or w.current_input_id != input_id:
            return
        if not w.abort(reason):
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.abort_wait_s)
        except Exception as e:  # noqa: BLE001 - timeout or the task's own failure
            logger.warning("session %s: no settle after abort{%s}: %r", w.session_key, reason, e)

    # ------------------------------------------------------------ activities
    @activity.defn(name="prepare_vm")
    async def prepare_vm(self, inp: PrepareVM) -> None:
        w = self.worker(inp.session)
        self._started(w, "prepare_vm", inp.input_id, resume_from_park=inp.resume_from_park)
        await w.ensure_ready()

    @activity.defn(name="run_task")
    async def run_task(self, inp: RunTask) -> RunResult:
        info = activity.info()
        w = self.worker(inp.session)
        extra: dict = {"resume": inp.resume}
        if info.attempt > 1:
            extra["floor_seq"] = _floor_seq(info.heartbeat_details)
        self._started(w, "run_task", inp.input_id, **extra)
        w.wall_clock_s = inp.wall_clock_s
        hb = asyncio.get_running_loop().create_task(self._heartbeat(w))
        try:
            if inp.resume and inp.question_id:
                # a parked guest is restored, ui_response first; a retry
                # resends the same input_id and the guest dedupes it
                sent = w.answer(inp.question_id, inp.answer or "",
                                answer_input_id(inp.input_id, inp.question_id))
                if not sent and inp.input_id not in w.sync.settled:
                    # no guest holds the parked task: start_task would run
                    # it again from scratch in a fresh run (a second turn)
                    raise ApplicationError(
                        f"no guest to resume input {inp.input_id} (question {inp.question_id})",
                        non_retryable=True)
            # a running act() for this input is re-attached, a settled one
            # replays its stored settle: never a second turn
            w.start_task({"input_id": inp.input_id, "text": inp.text, "kind": inp.kind})
            res = await w.wait_outcome(inp.input_id)
        except asyncio.CancelledError:
            # a workflow abort or the activity's timeout; not our own
            # shutdown (the retry on the next worker re-attaches)
            if w.current_input_id == inp.input_id and not activity.is_worker_shutdown():
                await self._abort_and_wait(w, inp.input_id, "aborted")
            raise
        finally:
            hb.cancel()
        outcome = res.get("outcome")
        if outcome == "parked":
            return RunResult(outcome="parked", question_id=res.get("question_id"),
                             last_seq=w.sync.last_seq)
        return RunResult(outcome=outcome if outcome in SETTLE_OUTCOMES else "failed",
                         question_id=inp.question_id, last_seq=w.sync.last_seq)

    @activity.defn(name="park_vm")
    async def park_vm(self, inp: ParkVM) -> None:
        w = self.worker(inp.session)
        self._started(w, "park_vm", inp.input_id, question_id=inp.question_id)
        if not await w.ensure_parked("question"):
            logger.warning("session %s: could not park for question %s (vm %s)",
                           w.session_key, inp.question_id, w.vm_state)

    @activity.defn(name="deliver")
    async def deliver(self, inp: Deliver) -> None:
        w = self.worker(inp.session)
        iid = inp.input_id
        self._started(w, "deliver", iid, outcome=inp.outcome)
        if iid not in w.sync.settled and inp.outcome in ("escalated", "aborted"):
            # no guest settle (the answer timer, an abort while parked): wake
            # the guest and let it settle the task itself (M5 §4)
            if w.task_for(iid) is None and w.vm_state == "parked":
                w.abort(inp.outcome)
                # a restarted agent-api: re-attach (the guest dedupes the task)
                w.start_task({"input_id": iid, "text": ""})
                task = w.task_for(iid)
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=self.abort_wait_s)
                except Exception as e:  # noqa: BLE001
                    logger.warning("session %s: no settle after abort: %r", w.session_key, e)
            else:
                await self._abort_and_wait(w, iid, inp.outcome)
        settle = w.sync.settled.get(iid) or {}
        act = w.task_for(iid)
        if not settle and act is not None and act.done() and not act.cancelled() \
                and act.exception() is None:
            settle = act.result()               # the host's own failure result
        seq = w.sync.settled_seq.get(iid)
        ticket_id = ticket_id_for(inp.session, iid, seq)
        path = w.session.tickets / f"{ticket_id}.json"
        first = not path.exists()
        atomic_write_json(path, {
            "ticket_id": ticket_id, "input_id": iid, "outcome": inp.outcome,
            "answer": settle.get("answer", ""), "question_id": inp.question_id,
            "manifest": settle.get("manifest", {}), "usage": settle.get("usage", {}),
            "settled_seq": seq,
        })
        w.end_input_stream(iid, inp.outcome)
        w.forget_task(iid)
        if first:
            w.sync.host_event("workflow", {"kind": "delivered", "input_id": iid,
                                           "outcome": inp.outcome, "ticket_id": ticket_id})
