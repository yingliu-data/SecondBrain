import asyncio
import contextlib
import logging

from fastapi import APIRouter, Request, Depends
from flows.activities import SessionRef, TaskSignal
from flows.client import signal_answer, signal_task
from flows.session_workflow import AnswerSignal
from fastapi.responses import StreamingResponse
from app.auth.middleware import verify
from app.agent.loop import run_agent_loop, SSE_HEADERS
from app.config import AGENT_ENGINE, HARNESS_KEEPALIVE_S, MAX_INPUT
from app.host import temporal
from app.host.activities import bind_workflow
from app.host.stream import DONE, KEEPALIVE, sse, tokens
from app.host.worker import Workers, new_input_id
from app.session.dir_store import DirStore
from app.session.factory import create_session_store
from app.session.queue import SessionQueue
from app.session.ticket import Ticket
from app.skills.base import set_current_context
from app.tenants import Tenant
from app.user.context_builder import ContextBuilder

logger = logging.getLogger("chat")

router = APIRouter()

sessions = create_session_store()
session_queue = SessionQueue()
workers = Workers()          # harness engine: one Worker per running session
_registry = None
_llm = None


def set_dependencies(registry, llm):
    global _registry, _llm
    _registry = registry
    _llm = llm


def engine_for(tenant: Tenant) -> str:
    """`engine` in tenants.json overrides AGENT_ENGINE (PLAN.md D3)."""
    return tenant.engine or AGENT_ENGINE


def uses_harness(tenant: Tenant) -> bool:
    """The harness engine needs the directory session store; otherwise the
    tenant falls back to the legacy loop (and its tool-result path)."""
    return engine_for(tenant) == "harness" and isinstance(sessions, DirStore)


@router.post("/api/v1/chat")
async def chat(request: Request, tenant: Tenant = Depends(verify)):
    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT]
    session_id = body.get("session_id", "default")

    if uses_harness(tenant) and temporal.enabled():
        input_id = body.get("input_id") or new_input_id()
        generator = _workflow_turn(tenant, session_id, message, input_id,
                                   answer_to=body.get("answer_to"))
    elif uses_harness(tenant):
        input_id = body.get("input_id") or new_input_id()
        generator = _harness_turn(tenant, session_id, message, input_id,
                                  answer_to=body.get("answer_to"))
    elif isinstance(sessions, DirStore):
        generator = _dir_turn(tenant, session_id, message)
    else:
        generator = _legacy_turn(tenant, session_id, message)

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _dir_turn(tenant: Tenant, session_id: str, message: str):
    """One chat turn against a session directory: serialized per session,
    ticketed, fully traced; history survives client disconnects."""
    session = sessions.get_or_create(tenant.user, tenant.name, session_id)

    async with session_queue.run(f"{tenant.user}/{tenant.name}_{session_id}"):
        ticket = Ticket.start(session, "chat.turn", inputs={"message": message})
        set_current_context(session, tenant.user, tenant.name, session_id)

        system = ContextBuilder(
            user_id=tenant.user, session=session,
            base_prompt=tenant.system_prompt,
        ).build_system(message)

        session.append_history("user", message, ticket_id=ticket.ticket_id)
        history = session.read_history(limit=20)
        # run_agent_loop appends the user msg itself; hand it prior history only
        history = history[:-1]

        def trace(event, detail=None, duration_ms=None):
            if duration_ms is not None:
                detail = {**(detail or {}), "duration_ms": duration_ms}
            try:
                session.append_trace(event, detail, ticket_id=ticket.ticket_id)
            except Exception as e:  # tracing must never break a turn
                logger.warning(f"trace write failed: {e}")

        turn_history: list = list(history)
        n_before = len(turn_history)
        completed = False
        try:
            async for event in run_agent_loop(
                message, turn_history, _registry, _llm,
                system_prompt=system,
                max_tools=tenant.max_tools,
                max_tokens=tenant.max_tokens,
                allowed_skills=tenant.allowed_skill_names(),
                trace=trace,
            ):
                yield event
            completed = True
        finally:
            # Persist whatever the loop appended (assistant reply), even if
            # the client disconnected mid-stream.
            for msg in turn_history[n_before + 1:]:
                if msg.get("role") == "assistant":
                    session.append_history(
                        "assistant", msg.get("content") or "",
                        ticket_id=ticket.ticket_id,
                    )
            if not completed:
                trace("client_disconnected")
            ticket.finish(
                "success" if completed else "failed",
                summary=f"turn: {message[:80]}",
            )


async def _legacy_turn(tenant: Tenant, session_id: str, message: str):
    """sqlite/dict fallback — original behavior."""
    store_key = tenant.session_key(session_id)
    history = sessions.setdefault(store_key, [])
    set_current_context(None, tenant.user, tenant.name, session_id)
    async for event in run_agent_loop(
        message, history, _registry, _llm,
        system_prompt=tenant.system_prompt,
        max_tools=tenant.max_tools,
        max_tokens=tenant.max_tokens,
        allowed_skills=tenant.allowed_skill_names(),
    ):
        yield event
    if hasattr(sessions, "save"):
        sessions.save(store_key, history)


async def _harness_turn(tenant: Tenant, session_id: str, message: str, input_id: str,
                        *, answer_to: str | None = None):
    """One input through the harness engine: the session's Worker runs the
    task in the sandbox; this generator streams the Worker's SSE sink and
    keeps the connection alive. The task outlives a client disconnect; a
    redelivered `input_id` attaches to the running task or gets the stored
    answer again. An answer (`answer_to`) does not queue behind the session:
    the task it answers holds the queue while it waits for it (the Worker
    delivers it as `ui_response`, restoring a parked guest first, M3)."""
    session = sessions.get_or_create(tenant.user, tenant.name, session_id)

    queue_cm = (contextlib.nullcontext() if answer_to
                else session_queue.run(f"{tenant.user}/{tenant.name}_{session_id}"))
    async with queue_cm:
        ticket = Ticket.start(session, "chat.turn",
                              inputs={"message": message, "input_id": input_id})
        set_current_context(session, tenant.user, tenant.name, session_id)
        worker = workers.get_or_create(session, tenant, _registry)
        sink = worker.attach(input_id)
        task_input = {"input_id": input_id, "text": message, "ticket_id": ticket.ticket_id}
        if answer_to:
            task_input["answer_to"] = answer_to
        task = asyncio.ensure_future(worker.act(task_input))

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
        completed = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(sink.get(), timeout=HARNESS_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    if task.done() and sink.empty():
                        break
                    yield KEEPALIVE
                    continue
                if item is None:
                    break
                yield item
            completed = True
        finally:
            worker.detach(sink)
            if not completed:
                try:
                    session.append_trace("client_disconnected", ticket_id=ticket.ticket_id)
                except Exception as e:
                    logger.warning(f"trace write failed: {e}")


async def _workflow_turn(tenant: Tenant, session_id: str, message: str, input_id: str,
                         *, answer_to: str | None = None):
    """One input through the session workflow (M5-CONTRACTS §3): a `task`
    signal-with-start, or for `answer_to` the `answer` signal when the
    workflow is parked on that question (else ui_response to the live
    worker). The response streams the Worker's events for the task's
    input_id until `deliver` ends them; it is a projection only, a client
    disconnect never touches the task."""
    session = sessions.get_or_create(tenant.user, tenant.name, session_id)
    set_current_context(session, tenant.user, tenant.name, session_id)
    worker = workers.get_or_create(session, tenant, _registry)
    config = bind_workflow(worker, tenant)
    ref = SessionRef(user=tenant.user, tenant=tenant.name, session_id=session_id)
    no_question = [sse("error", {"class": "fatal", "message": "no question is waiting"}), DONE]

    if answer_to:
        st = await temporal.status(ref)
        task_id = (st.current_input_id if st is not None else None) or worker.current_input_id
        if task_id is None:
            for item in tokens("No question is waiting for this answer.") + no_question:
                yield item
            return
        if st is not None and st.state == "parked" and st.question_id != answer_to:
            # the workflow holds the parked task and ignores this answer:
            # a ui_response to the worker would wake the guest behind its back
            for item in no_question:
                yield item
            return
    sink = worker.attach(task_id if answer_to else input_id)
    try:
        if answer_to:
            if st is not None and st.state == "parked":
                await signal_answer(temporal.client, ref, AnswerSignal(question_id=answer_to,
                                                                       text=message))
            elif not worker.answer(answer_to, message, input_id):
                for item in no_question:
                    yield item
                return
        elif not worker.replay_to(sink, input_id):      # a settled input: its answer again
            await signal_task(temporal.client, ref, TaskSignal(input_id=input_id, text=message),
                              task_queue=temporal.task_queue, config=config)
        while True:
            try:
                item = await asyncio.wait_for(sink.get(), timeout=HARNESS_KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield KEEPALIVE
                continue
            if item is None:
                break
            yield item
    finally:
        worker.detach(sink)
