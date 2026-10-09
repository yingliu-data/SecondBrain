"""POST /api/v1/chat — one input through the harness engine (every tenant,
M6): the session workflow when Temporal is up, else the direct path
(Worker.act). `turn_stream` is shared with the guest avatar route."""
import asyncio
import logging

from fastapi import APIRouter, Request, Depends
from flows.activities import SessionRef, TaskSignal
from flows.client import signal_answer, signal_task
from flows.session_workflow import AnswerSignal
from fastapi.responses import StreamingResponse
from app.auth.middleware import verify
from app.config import HARNESS_KEEPALIVE_S, MAX_INPUT, SESSION_BACKEND
from app.host import temporal
from app.host.activities import session_config
from app.host.stream import KEEPALIVE, SSE_HEADERS, fatal_items, tokens
from app.host.worker import Workers, new_input_id
from app.session.dir_store import DirStore
from app.session.factory import create_session_store
from app.session.ids import session_key
from app.session.queue import SessionQueue
from app.session.ticket import Ticket
from app.skills.base import set_current_context
from app.tenants import Tenant

logger = logging.getLogger("chat")

router = APIRouter()


def _harness_store() -> DirStore:
    """The directory session store: the only one the harness engine runs on
    (M6-CONTRACTS §3). Anything else stops agent-api at import (startup)."""
    store = create_session_store()
    if not isinstance(store, DirStore):
        raise RuntimeError(
            f"SESSION_BACKEND={SESSION_BACKEND!r} is not supported: the harness engine "
            f"needs the directory session store (SESSION_BACKEND=dir, the default). "
            f"The legacy engine that ran on sqlite/memory was removed in M6; migrate "
            f"old sqlite sessions with app/session/migrate.py.")
    return store


sessions = _harness_store()
session_queue = SessionQueue()
workers = Workers(sessions=sessions)   # one Worker per running session
_registry = None


def set_registry(registry):
    global _registry
    _registry = registry


def turn_stream(tenant: Tenant, session_id: str, message: str, input_id: str | None = None,
                *, answer_to: str | None = None):
    """The SSE items of one input of `tenant`'s session: the workflow path
    when Temporal is up, else the direct path. A missing input_id is minted."""
    input_id = input_id or new_input_id()
    if temporal.enabled():
        return _workflow_turn(tenant, session_id, message, input_id, answer_to=answer_to)
    return _harness_turn(tenant, session_id, message, input_id, answer_to=answer_to)


@router.post("/api/v1/chat")
async def chat(request: Request, tenant: Tenant = Depends(verify)):
    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT]
    session_id = body.get("session_id", "default")
    generator = turn_stream(tenant, session_id, message, body.get("input_id"),
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


async def _answer_stream(worker, task_id: str | None, answer_to: str, message: str,
                         input_id: str):
    """An answer to question `answer_to` as ui_response to the worker's
    live or parked guest (a parked one is restored first, M3), then the
    stream of `task_id`, the task it belongs to; NO_QUESTION when there is
    no such task or no guest takes it."""
    sink = worker.attach(task_id) if task_id is not None else None
    if sink is None or worker.answer(answer_to, message, input_id) == "no_guest":
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
    Worker runs the task in the sandbox (act(), the in-process driver);
    this generator streams the Worker's SSE sink and keeps the connection
    alive. The task outlives a client disconnect; a redelivered `input_id`
    attaches to the running task or gets the stored answer again. An
    answer (`answer_to`) does not queue behind the session: it goes to the
    running task as ui_response (a parked guest is restored first, M3) and
    streams that task, as on the workflow path."""
    worker = workers.get_or_create(tenant, session_id, _registry)
    session = worker.session
    set_current_context(session, tenant.user, tenant.name, session_id)
    if answer_to:
        await worker.settle_park()      # a question park in progress ends first
        qp = worker.question_park
        # parked on another question: a ui_response would wake the guest
        # behind act()'s answer wait (as on the workflow path)
        task_id = worker.current_input_id if qp is None or qp["question_id"] == answer_to else None
        async for item in _answer_stream(worker, task_id, answer_to,
                                         message, input_id):
            yield item
        return

    async with session_queue.run(session_key(tenant.user, tenant.name, session_id)):
        ticket = await asyncio.to_thread(Ticket.start, session, "chat.turn",
                                         inputs={"message": message, "input_id": input_id})
        sink = worker.attach(input_id)
        task = asyncio.ensure_future(worker.act({"input_id": input_id, "text": message,
                                                 "ticket_id": ticket.ticket_id}))

        def _finish(t: asyncio.Task) -> None:
            try:
                result = t.result()
                state = "success" if result.get("outcome") == "done" else "failed"
                summary = f"turn: {message[:80]} ({result.get('outcome')})"
            except BaseException as e:  # noqa: BLE001 - the ticket records any failure
                state, summary = "failed", f"turn: {message[:80]} ({e})"
            try:
                ticket.finish(state, summary=summary)
            except Exception as e:
                logger.warning(f"ticket finish failed: {e}")

        task.add_done_callback(_finish)
        async for item in _drain(worker, sink, task):
            yield item


async def _workflow_turn(tenant: Tenant, session_id: str, message: str, input_id: str,
                         *, answer_to: str | None = None):
    """One input through the session workflow (M5-CONTRACTS §3): a `task`
    signal-with-start, or for `answer_to` the `answer` signal when the
    workflow is parked on that question (else ui_response to the live
    worker). The response streams the Worker's events for the task's
    input_id until `deliver` ends them; it is a projection only, a client
    disconnect never touches the task."""
    worker = workers.get_or_create(tenant, session_id, _registry)
    set_current_context(worker.session, tenant.user, tenant.name, session_id)
    ref = SessionRef(user=tenant.user, tenant=tenant.name, session_id=session_id)

    if not answer_to:
        sink = worker.attach(input_id)
        if worker.replay(input_id, sink) is None:       # a settled input: its answer again
            await signal_task(temporal.client, ref, TaskSignal(input_id=input_id, text=message),
                              task_queue=temporal.task_queue, config=session_config(tenant))
        async for item in _drain(worker, sink):
            yield item
        return

    await worker.settle_park()          # a question park in progress ends first
    qp = worker.question_park
    if qp is not None and qp["question_id"] == answer_to:
        # this process's worker is parked on that question: no status query
        task_id, parked = qp["input_id"], True
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
        async for item in _answer_stream(worker, task_id, answer_to, message, input_id):
            yield item
        return
    sink = worker.attach(task_id)
    await signal_answer(temporal.client, ref, AnswerSignal(question_id=answer_to, text=message))
    async for item in _drain(worker, sink):
        yield item
