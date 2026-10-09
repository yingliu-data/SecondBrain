"""Guest chat endpoint — unauthenticated (auth: origin), avatar_control only.

M6 (PLAN.md 8 "Guest tenant (avatar)"): the route's own loop is gone; the
input goes through the same harness chat path as every tenant
(routes/chat.turn_stream) with the guest tenant profile (app/auth/guest.py).
This route keeps only what is the page's: the Origin check, the page's
limits (GuestSessionManager) and the wire shape the page consumes —
`token`, `avatar_command`, `done` (and keepalive comments); the harness's
other events (`seq`, `question`, `tool_call`, `error`) are not sent.
"""
import hashlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.guest import MAX_INPUT_CHARS, guest_manager
from app.host.stream import SSE_HEADERS
from app.routes import chat
from app.session.ids import is_safe_id, session_key

logger = logging.getLogger("guest")
router = APIRouter()

_tenant = None      # the guest profile (TenantRegistry.guest(), set by main.py)

# the page's wire: these events and SSE comments (keepalive) pass, the rest is dropped
_PASS = ("event: token\n", "event: avatar_command\n", "event: done\n", ":")


def set_tenant(tenant) -> None:
    global _tenant
    _tenant = tenant
    guest_manager.on_expire = _close_session


def harness_session_id(page_session_id: str) -> str:
    """The page's session id as a harness session id (a path segment): kept
    when it is safe, else a stable digest of it."""
    if is_safe_id(page_session_id):
        return page_session_id
    return "g_" + hashlib.sha256(page_session_id.encode()).hexdigest()[:32]


async def _close_session(page_session_id: str) -> None:
    """An expired guest session's Worker is closed (its sandbox stopped)."""
    if _tenant is None:
        return
    key = session_key(_tenant.user, _tenant.name, harness_session_id(page_session_id))
    try:
        await chat.workers.close(key)
    except Exception as e:  # noqa: BLE001 - cleanup must not stop the loop
        logger.warning("guest session %s: close failed: %s", key, e)


async def _page_view(items):
    async for item in items:
        if item.startswith(_PASS):
            yield item


@router.post("/api/v1/guest/chat")
async def guest_chat(request: Request):
    origin = request.headers.get("origin", "")
    if _tenant is None or origin not in _tenant.origins:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT_CHARS]
    session_id = body.get("session_id", "")

    if not session_id or not message.strip():
        return JSONResponse({"error": "Missing session_id or message"}, status_code=400)

    ip = request.client.host if request.client else "unknown"
    session, error = guest_manager.get_or_create(session_id, ip)
    if not session:
        return JSONResponse({"error": error}, status_code=429)

    session.message_count += 1
    items = chat.turn_stream(_tenant, harness_session_id(session_id), message)
    return StreamingResponse(_page_view(items), media_type="text/event-stream",
                             headers=SSE_HEADERS)
