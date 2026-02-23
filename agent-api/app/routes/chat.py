from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse
from app.auth.middleware import verify
from app.agent.loop import run_agent_loop, SSE_HEADERS
from app.config import MAX_INPUT
from app.session.factory import create_session_store

router = APIRouter()

sessions = create_session_store()
_registry = None
_llm = None


def set_dependencies(registry, llm):
    global _registry, _llm
    _registry = registry
    _llm = llm


@router.post("/api/v1/chat", dependencies=[Depends(verify)])
async def chat(request: Request):
    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT]
    session_id = body.get("session_id", "default")
    history = sessions.setdefault(session_id, [])

    async def generate_and_save():
        async for event in run_agent_loop(message, history, _registry, _llm):
            yield event
        if hasattr(sessions, "save"):
            sessions.save(session_id, history)

    return StreamingResponse(
        generate_and_save(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
