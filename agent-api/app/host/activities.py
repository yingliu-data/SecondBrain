"""activities — the real bodies of the session workflow's activities.

Same names and payloads as the stubs in
flows/activities.py; flows.worker.build_worker registers them (bound
methods of one HostActivities) next to flows' own `signal_session` glue.
They drive the Worker's task API, as act() does on the direct path.

  prepare_vm(PrepareVM)        Worker for the session; boot / wake the guest
  run_task(RunTask)->RunResult the input's worker-owned task (a retry
                               re-attaches by input_id, never a second turn);
                               heartbeat {last_seq} at start and on every tick
                               of the task's heartbeat (Worker.on_heartbeat);
                               resume = ui_response, then wait; returns at the
                               settle or at the guest's question park;
                               cancelled (workflow abort) -> abort{aborted}
                               to the guest, wait for its settle, re-raise
  park_vm(ParkVM)              parked + snapshotted (normally already: the
                               worker adopted the guest's own park)
  deliver(Deliver)             escalated/aborted with no settle: wake + abort
                               so the guest writes it; the ticket projection
                               {session}/tickets/{ticket_id}.json; finish_input
                               (SSE error unless done, then done, end of
                               stream); host event workflow{delivered}.
                               Idempotent.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from flows.activities import Deliver, ParkVM, PrepareVM, RunResult, RunTask, SessionRef
from protocol.canonical import derive_key_id

from app.host.worker import Worker, end_error, settle_outcome, task_ok
from app.util.ipc import atomic_write_json

logger = logging.getLogger("host.activities")


def answer_input_id(input_id: str, question_id: str) -> str:
    """The ui_response's input_id for a workflow `answer`: stable across
    retries, so the guest dedupes a resent answer."""
    return f"{input_id}:answer:{question_id}"


def ticket_id_for(ref: SessionRef, input_id: str) -> str:
    """The ticket of one input: `ticket_` + 24 hex of the canonical hash of
    (user, tenant, session_id, input_id). Not the settle seq: seqs restart
    with every guest run."""
    return derive_key_id("ticket", ref.user, ref.tenant, ref.session_id, input_id)


def _floor_seq(details: Any) -> int:
    """The previous attempt's last heartbeat {last_seq} (a floor only: the
    SDK throttles heartbeats)."""
    try:
        d = details[0] if details else None
        return int(d.get("last_seq", 0)) if isinstance(d, dict) else int(d or 0)
    except (TypeError, ValueError, IndexError):
        return 0


class HostActivities:
    def __init__(self, workers, tenants, registry) -> None:
        self.workers = workers          # app.host.worker.Workers (over the DirStore)
        self.tenants = tenants          # TenantRegistry (get_by_name)
        self.registry = registry        # SkillRegistry

    def activities(self) -> list:
        return [self.prepare_vm, self.run_task, self.park_vm, self.deliver]

    # ------------------------------------------------------------ helpers
    def worker(self, ref: SessionRef) -> Worker:
        tenant = self.tenants.get_by_name(ref.tenant)
        if tenant is None or tenant.user != ref.user:
            raise ApplicationError(f"unknown tenant {ref.user}/{ref.tenant}", non_retryable=True)
        return self.workers.get_or_create(tenant, ref.session_id, self.registry)

    @staticmethod
    async def _started(w: Worker, name: str, input_id: str, **extra) -> None:
        await asyncio.to_thread(w.sync.host_event, "workflow", {
            "kind": "activity_start", "activity": name, "input_id": input_id,
            "attempt": activity.info().attempt, **extra})

    # ------------------------------------------------------------ activities
    @activity.defn(name="prepare_vm")
    async def prepare_vm(self, inp: PrepareVM) -> None:
        w = self.worker(inp.session)
        await self._started(w, "prepare_vm", inp.input_id, resume_from_park=inp.resume_from_park)
        await w.ensure_ready()

    @activity.defn(name="run_task")
    async def run_task(self, inp: RunTask) -> RunResult:
        info = activity.info()
        w = self.worker(inp.session)
        extra: dict = {"resume": inp.resume}
        if info.attempt > 1:
            extra["floor_seq"] = _floor_seq(info.heartbeat_details)
        await self._started(w, "run_task", inp.input_id, **extra)
        # the task's heartbeat ticks in its own asyncio task: run there in
        # this activity's context (activity.heartbeat reads a contextvar)
        ctx = contextvars.copy_context()

        def beat() -> None:
            ctx.run(activity.heartbeat, {"last_seq": w.sync.last_seq})
        beat()
        off = w.on_heartbeat(inp.input_id, beat)
        try:
            if inp.resume and inp.question_id:
                # a parked guest is restored, ui_response first; a retry
                # resends the same input_id and the guest dedupes it
                sent = w.answer(inp.question_id, inp.answer or "",
                                answer_input_id(inp.input_id, inp.question_id))
                if sent == "no_guest" and inp.input_id not in w.sync.settled:
                    # no guest holds the parked task: start_task would run
                    # it again from scratch in a fresh run (a second turn)
                    raise ApplicationError(
                        f"no guest to resume input {inp.input_id} (question {inp.question_id})",
                        non_retryable=True)
            # a running task for this input is re-attached, a settled one
            # returns its stored settle: never a second turn
            w.start_task({"input_id": inp.input_id, "text": inp.text, "kind": inp.kind,
                          "wall_clock_s": inp.wall_clock_s})
            res = await w.wait_outcome(inp.input_id)
        except asyncio.CancelledError:
            # a workflow abort or the activity's timeout; not our own
            # shutdown (the retry on the next worker re-attaches)
            if not activity.is_worker_shutdown():
                await w.abort_and_wait(inp.input_id, "aborted")
            raise
        finally:
            off()
        outcome = res.get("outcome")
        if outcome == "parked":
            return RunResult(outcome="parked", question_id=res.get("question_id"),
                             last_seq=w.sync.last_seq)
        return RunResult(outcome=settle_outcome(outcome),
                         question_id=inp.question_id, last_seq=w.sync.last_seq)

    @activity.defn(name="park_vm")
    async def park_vm(self, inp: ParkVM) -> None:
        w = self.worker(inp.session)
        await self._started(w, "park_vm", inp.input_id, question_id=inp.question_id)
        if not await w.ensure_parked("question"):
            logger.warning("session %s: could not park for question %s (vm %s)",
                           w.session_key, inp.question_id, w.vm_state)

    @activity.defn(name="deliver")
    async def deliver(self, inp: Deliver) -> None:
        w = self.worker(inp.session)
        iid = inp.input_id
        await self._started(w, "deliver", iid, outcome=inp.outcome)
        if iid not in w.sync.settled and inp.outcome in ("escalated", "aborted"):
            # no guest settle (the answer timer, an abort while parked): wake
            # the guest and let it settle the task itself
            await w.abort_and_wait(iid, inp.outcome, reattach=True,
                                   question_id=inp.question_id if inp.outcome == "escalated"
                                   else None)
        act = w.task_for(iid)
        settle = w.sync.settled.get(iid) or (act.result() if act is not None and task_ok(act)
                                             else {})     # else: the host's own failure result
        ticket_id = ticket_id_for(inp.session, iid)
        path = w.session.tickets / f"{ticket_id}.json"
        first = not path.exists()
        await asyncio.to_thread(atomic_write_json, path, {
            "ticket_id": ticket_id, "input_id": iid, "outcome": inp.outcome,
            "answer": settle.get("answer", ""), "question_id": inp.question_id,
            "manifest": settle.get("manifest", {}), "usage": settle.get("usage", {}),
        })
        w.finish_input(iid, end_error(act, inp.outcome), outcome=inp.outcome)
        if first:
            await asyncio.to_thread(w.sync.host_event, "workflow", {
                "kind": "delivered", "input_id": iid, "outcome": inp.outcome,
                "ticket_id": ticket_id})
