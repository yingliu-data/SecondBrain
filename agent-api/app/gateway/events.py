"""POST /internal/gateway-events — the gateway's events into the session's
host log (M4-CONTRACTS §7).

Body `{user, tenant, session_id, op, tool?, upstream?, ok?, ms?, error?}`,
bearer INTERNAL_TOKEN (empty token = no check). The event is appended as a
`gateway` host event (body = the payload without user/tenant/session_id) to
`{SESSIONS_ROOT}/{user}/{tenant}_{session_id}/events/host-*.jsonl`:

  - a live worker for the session: through its StateSync.host_event (one
    writer, one hseq counter)
  - no worker: HostLogWriter, serialised by a per-session lock, recounting
    hseq from the host segments on disk (never a second StateSync)

Answers: 202 `{"stored": true}`; 404 `{"stored": false}` when there is no
such session directory (dropped, logged: the gateway logs it and moves on —
the post is fire-and-forget); 400 malformed body or unsafe ids; 401 bad
bearer; 503 when sessions are not directory-backed.
"""
from __future__ import annotations

import hmac
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import INTERNAL_TOKEN
from app.host.state_sync import append_host_event, count_host_events
from app.session.ids import is_safe_id

logger = logging.getLogger("gateway.events")

router = APIRouter()

IDENTITY = ("user", "tenant", "session_id")


class HostLogWriter:
    """Appends host events to sessions without a live worker. One lock per
    session directory; hseq recounted from disk under it on every append."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Path, threading.Lock] = {}

    def _lock(self, session_dir: Path) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_dir, threading.Lock())

    def append(self, session_dir: Path, session_id: str, type: str, body: dict) -> dict:
        events = Path(session_dir) / "events"
        with self._lock(Path(session_dir)):
            hseq = count_host_events(events) + 1
            return append_host_event(events, hseq, session_id, type, body)


HOST_LOG = HostLogWriter()


def _authorized(request: Request, token: str) -> bool:
    if not token:
        return True
    got = request.headers.get("Authorization", "")
    return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())


def record_gateway_event(payload: dict, *, sessions, workers, writer: HostLogWriter = HOST_LOG
                         ) -> bool:
    """Append one gateway event; False when the session directory is absent."""
    user, tenant, session_id = (payload[k] for k in IDENTITY)
    session_dir = sessions.path_for(user, tenant, session_id)
    if not (session_dir / "session.json").exists():
        logger.info("gateway event for unknown session %s/%s_%s dropped (%s)",
                    user, tenant, session_id, payload.get("op"))
        return False
    body = {k: v for k, v in payload.items() if k not in IDENTITY}
    worker = workers.get(f"{user}/{tenant}_{session_id}")
    if worker is not None and not worker.closed:
        worker.sync.host_event("gateway", body)
    else:
        writer.append(session_dir, session_id, "gateway", body)
    return True


@router.post("/internal/gateway-events")
async def gateway_events(request: Request):
    from app.routes import chat            # the process's session store and workers
    from app.session.dir_store import DirStore

    if not _authorized(request, INTERNAL_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    if (not isinstance(payload, dict)
            or not all(isinstance(payload.get(k), str) and is_safe_id(payload[k]) for k in IDENTITY)
            or not isinstance(payload.get("op"), str)):
        return JSONResponse({"error": "need safe user, tenant, session_id and an op"},
                            status_code=400)
    if not isinstance(chat.sessions, DirStore):
        return JSONResponse({"error": "no session directories"}, status_code=503)
    stored = record_gateway_event(payload, sessions=chat.sessions, workers=chat.workers)
    return JSONResponse({"stored": stored}, status_code=202 if stored else 404)
