"""POST /api/v1/chat — one input through the harness engine (every tenant).

The path is configuration (temporal.configured(): TEMPORAL_ADDRESS set):
the session workflow only, else the direct path (Worker.act) only; never a
switch mid-session. While the configured Temporal is connecting or down the
request waits TEMPORAL_READY_WAIT_S, then answers 503 + Retry-After.
`open_turn` is shared with the guest avatar route."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, Depends
from flows.activities import TaskSignal
from flows.client import signal_answer, signal_task
from flows.session_workflow import AnswerSignal
from fastapi.responses import StreamingResponse
from app.auth.middleware import verify
from app.config import (HARNESS_KEEPALIVE_S, MAX_INPUT, TEMPORAL_READY_WAIT_S,
                        TEMPORAL_RETRY_AFTER_S)
from app.host import temporal
from app.host.stream import KEEPALIVE, SSE_HEADERS, fatal_items, tokens
from app.host.worker import Workers, new_input_id, session_config
from app.session.factory import create_session_store
from app.session.ids import is_safe_id, session_ref
from app.skills.base import set_current_context
from app.tenants import Tenant

logger = logging.getLogger("chat")

router = APIRouter()


sessions = create_session_store()
workers = Workers(sessions=sessions)   # one Worker per running session; the task cap
_registry = None


def set_registry(registry):
    global _registry
    _registry = registry


async def open_turn(tenant: Tenant, session_id: str, message: str,
                    input_id: str | None = None, *, answer_to: str | None = None):
    """The SSE items of one input of `tenant`'s session, on the configured
    path. Raises HTTPException 400 for an unsafe session id, 503 (+
    Retry-After) when the workflow path is configured but Temporal is not
    ready within TEMPORAL_READY_WAIT_S. A missing input_id is minted."""
    if not is_safe_id(session_id):
        raise HTTPException(400, "Invalid session id")
    input_id = input_id or new_input_id()
    if not temporal.configured():
        return _harness_turn(tenant, session_id, message, input_id, answer_to=answer_to)
    await require_workflow_ready()
    return _workflow_turn(tenant, session_id, message, input_id, answer_to=answer_to)


async def require_workflow_ready() -> None:
    """Temporal ready within TEMPORAL_READY_WAIT_S, else HTTPException 503 +
    Retry-After (the workflow path is configured, its service is not up)."""
    if not await temporal.wait_ready(TEMPORAL_READY_WAIT_S):
        raise HTTPException(503, "The session workflow service is not available; retry.",
                            headers={"Retry-After": str(TEMPORAL_RETRY_AFTER_S)})


@router.post("/api/v1/chat")
async def chat(request: Request, tenant: Tenant = Depends(verify)):
    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT]
    session_id = body.get("session_id", "default")
    generator = await open_turn(tenant, session_id, message, body.get("input_id"),
                                answer_to=body.get("answer_to"))
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


NO_QUESTION = "No question is waiting for this answer."


async def _error_stream(message: str):
    """A request that runs nothing: its message as the answer, then a fatal
    error + done."""
    for item in tokens(message) + fatal_items(message):
        yield item


async def _drain(worker, sink, task: asyncio.Future | None = None):
    """The Worker's SSE items of one sink until its end of stream, with
    keepalives; with `task`, also stop once it is done and the sink is
    empty. The sink is detached at the end (or a client disconnect)."""
    try:
        while True:
            try:
                item = await asyncio.wait_for(sink.get(), timeout=HARNESS_KEEPALIVE_S)
            except asyncio.TimeoutError:
                if task is not None and task.done() and sink.empty():
                    return
                yield KEEPALIVE
                continue
            if item is None:
                return
            yield item
    finally:
        worker.detach(sink)


async def _answer_stream(worker, task_id: str | None, answer_to: str, message: str):
    """An answer to question `answer_to` as ui_response to the worker's
    live or parked guest (a parked one is restored first), then the
    stream of `task_id`, the task it belongs to (the ui_response's
    input_id derives from it: Worker.answer); NO_QUESTION when there is
    no such task, no guest takes it, or the guest is parked on another
    question (Worker.answer refuses it)."""
    sink = worker.attach(task_id) if task_id is not None else None
    if sink is None or worker.answer(answer_to, message, task_id) in ("no_guest",
                                                                      "other_question"):
        if sink is not None:
            worker.detach(sink)
        async for item in _error_stream(NO_QUESTION):
            yield item
        return
    async for item in _drain(worker, sink):
        yield item


async def _harness_turn(tenant: Tenant, session_id: str, message: str, input_id: str,
                        *, answer_to: str | None = None):
    """One input through the harness engine without Temporal: the session's
    Worker runs the task in the sandbox (act(), the in-process driver; the
    Worker serialises the session's tasks, its admission caps running
    tasks across sessions, its finish_input writes the ticket); this
    generator streams the Worker's SSE sink and keeps the connection
    alive. The task outlives a client disconnect; a redelivered `input_id`
    attaches to the running task or gets the stored answer again. An
    answer (`answer_to`) does not queue behind the session: it goes to the
    running task as ui_response (a parked guest is restored first) and
    streams that task, as on the workflow path."""
    worker = await workers.open(tenant, session_id, _registry)
    session = worker.session
    set_current_context(session, tenant.user, tenant.name, session_id)
    if answer_to:
        await worker.settle_park()      # a question park in progress ends first
        # the running task's; after a restart no task runs yet: the parked
        # question's (vm/parked.json), whose restored driver re-attaches it
        qp = worker.question_park
        task_id = worker.current_input_id or (qp["input_id"] if qp is not None else None)
        async for item in _answer_stream(worker, task_id, answer_to, message):
            yield item
        return

    sink = worker.attach(input_id)
    task = asyncio.ensure_future(worker.act({"input_id": input_id, "text": message}))
    async for item in _drain(worker, sink, task):
        yield item


async def _workflow_turn(tenant: Tenant, session_id: str, message: str, input_id: str,
                         *, answer_to: str | None = None):
    """One input through the session workflow: a `task`
    signal-with-start, or for `answer_to` the `answer` signal when the
    workflow is parked on that question (else ui_response to the live
    worker). The response streams the Worker's events for the task's
    input_id until `deliver` ends them; it is a projection only, a client
    disconnect never touches the task."""
    worker = await workers.open(tenant, session_id, _registry)
    set_current_context(worker.session, tenant.user, tenant.name, session_id)
    ref = session_ref(tenant.user, tenant.name, session_id)

    if not answer_to:
        sink = worker.attach(input_id)
        if worker.replay(input_id, sink) is None:       # a settled input: its answer again
            config = session_config(tenant)
            # the tenant's current wall clock (SessionConfig), as on the direct path
            task = TaskSignal(input_id=input_id, text=message, wall_clock_s=config.wall_clock_s)
            await signal_task(temporal.client, ref, task,
                              task_queue=temporal.task_queue, config=config)
        async for item in _drain(worker, sink):
            yield item
        return

    await worker.settle_park()          # a question park in progress ends first
    qp = worker.question_park
    if qp is not None:
        # this process's worker holds the park: on that question the
        # workflow takes the answer, on another Worker.answer refuses it
        task_id, parked = qp["input_id"], qp["question_id"] == answer_to
    else:
        st = await temporal.status(ref)
        task_id = (st.current_input_id if st is not None else None) or worker.current_input_id
        parked = st is not None and st.state == "parked"
        if task_id is None or (parked and st.question_id != answer_to):
            # nothing waits, or the workflow holds a task parked on another
            # question: a ui_response would wake the guest behind its back
            async for item in _error_stream(NO_QUESTION):
                yield item
            return
    if not parked:
        async for item in _answer_stream(worker, task_id, answer_to, message):
            yield item
        return
    sink = worker.attach(task_id)
    await signal_answer(temporal.client, ref, AnswerSignal(question_id=answer_to, text=message))
    async for item in _drain(worker, sink):
        yield item
