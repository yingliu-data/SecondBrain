import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse
from app.auth.middleware import verify
from app.agent.loop import run_agent_loop, SSE_HEADERS
from app.config import MAX_INPUT
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
_registry = None
_llm = None


def set_dependencies(registry, llm):
    global _registry, _llm
    _registry = registry
    _llm = llm


@router.post("/api/v1/chat")
async def chat(request: Request, tenant: Tenant = Depends(verify)):
    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT]
    session_id = body.get("session_id", "default")

    if isinstance(sessions, DirStore):
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
        set_current_context(session, tenant.user)

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
