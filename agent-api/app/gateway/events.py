"""POST /internal/gateway-events — the gateway's events into the session's
host log.

Body `{user, tenant, session_id, op, tool?, upstream?, ok?, ms?, error?}`,
bearer INTERNAL_TOKEN (empty token = no check). The event is appended as a
`gateway` host event (body = the payload without user/tenant/session_id) to
the session's directory (`DirStore.path_for`: `{SESSIONS_ROOT}/{user}/
{tenant}.{session_id}/`, or a legacy directory of exactly that session)
`events/host-*.jsonl` through
the directory's process-wide HostLog (state_sync.host_log): the same writer
and hseq counter a live worker's StateSync uses, so it does not matter
whether one is running.

Answers: 202 `{"stored": true}`; 404 `{"stored": false}` when there is no
such session directory (dropped, logged: the gateway logs it and moves on —
the post is fire-and-forget); 400 malformed body or unsafe ids; 401 bad
bearer. The file I/O runs in a worker thread, off the event loop.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from protocol.mcp import GATEWAY_EVENT_IDENTITY as IDENTITY, GATEWAY_EVENTS_PATH, is_safe_id

from app.config import INTERNAL_TOKEN
from app.gateway.client import bearer_ok
from app.host.state_sync import host_log

logger = logging.getLogger("gateway.events")

router = APIRouter()


def record_gateway_event(payload: dict, *, sessions) -> bool:
    """Append one gateway event; False when the session directory is absent."""
    user, tenant, session_id = (payload[k] for k in IDENTITY)
    session_dir = sessions.path_for(user, tenant, session_id)
    if sessions.get(user, tenant, session_id) is None:
        logger.info("gateway event for unknown session %s/%s/%s dropped (%s)",
                    user, tenant, session_id, payload.get("op"))
        return False
    body = {k: v for k, v in payload.items() if k not in IDENTITY}
    host_log(session_dir / "events").append_durable(session_id, "gateway", body)
    return True


@router.post(GATEWAY_EVENTS_PATH)
async def gateway_events(request: Request):
    from app.routes import chat            # the process's session store

    if not bearer_ok(request.headers.get("Authorization"), INTERNAL_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    if (not isinstance(payload, dict)
            or not all(is_safe_id(payload.get(k)) for k in IDENTITY)
            or not isinstance(payload.get("op"), str)):
        return JSONResponse({"error": "need safe user, tenant, session_id and an op"},
                            status_code=400)
    stored = await asyncio.to_thread(record_gateway_event, payload, sessions=chat.sessions)
    return JSONResponse({"stored": stored}, status_code=202 if stored else 404)
