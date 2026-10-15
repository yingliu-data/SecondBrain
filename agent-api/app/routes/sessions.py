import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends
from temporalio.service import RPCError, RPCStatusCode
from app.auth.middleware import verify
from flows.client import signal_abort
from app.host import temporal
from app.host.state_sync import host_log, read_current_run
from app.session.dir_store import DirStore
from app.session.eventlog import parse_ts
from app.session.ids import SessionRef, session_key, session_ref
from app.session.session_dir import SessionDir
from app.tenants import Tenant

router = APIRouter(prefix="/api/v1/sessions")
logger = logging.getLogger("routes.sessions")

_EPOCH = datetime.fromtimestamp(0, timezone.utc)

_sessions: DirStore | None = None


def set_sessions(sessions: DirStore):
    global _sessions
    _sessions = sessions


@router.get("")
async def list_sessions(tenant: Tenant = Depends(verify)):
    # a directory scan: off the loop
    rows = await asyncio.to_thread(_sessions.list_for_tenant, tenant.user, tenant.name)
    return {"sessions": [{"session_id": r["session_id"], "updated_at": r["updated_at"]}
                         for r in rows]}


def _delete(tenant: Tenant, session_id: str) -> bool:
    """The session directory removed, its host log's queued records
    written first (a record still queued would otherwise fail, or land in a
    session re-created under the same id)."""
    session = _sessions.get(tenant.user, tenant.name, session_id)
    if session is not None:
        host_log(session.events).flush()
    return _sessions.delete(tenant.user, tenant.name, session_id)


@router.delete("/{session_id}")
async def delete_session(session_id: str, tenant: Tenant = Depends(verify)):
    try:
        deleted = await asyncio.to_thread(_delete, tenant, session_id)
    except ValueError:
        raise HTTPException(400, "Invalid session id")
    if deleted:
        return {"status": "ok", "deleted": session_id}
    raise HTTPException(404, f"Session '{session_id}' not found")


def _since(minutes: int | None) -> datetime | None:
    if minutes is None:
        return None
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


@router.get("/traces/recent")
def recent_traces(since_minutes: int = 10, limit: int = 200,
                  tenant: Tenant = Depends(verify)):
    """Trace rows across all of this tenant's sessions — answers 'which
    tools ran in the last N minutes'. Built from each session's event log;
    a run (so a session) last written before the cutoff is not read. A
    plain def: FastAPI runs the file reads in its threadpool, off the
    event loop."""
    floor = _since(since_minutes)
    out = []
    for row in _sessions.list_for_tenant(tenant.user, tenant.name):
        try:
            # a session's own last `limit` rows are all it can add to the answer
            rows = SessionDir(row["root"]).read_trace(since=floor, limit=limit)
        except Exception as e:      # one unreadable log never hides the others
            logger.warning("traces: session %s unreadable: %s", row["session_id"], e)
            continue
        out.extend({"session_id": row["session_id"], **event} for event in rows)
    out.sort(key=lambda e: parse_ts(e.get("ts")) or _EPOCH)
    return {"traces": out[-limit:] if limit > 0 else []}


@router.get("/{session_id}/traces")
def session_traces(session_id: str, since_minutes: int | None = None,
                   limit: int = 200, tenant: Tenant = Depends(verify)):
    """One session's trace rows from its event log (newest runs first, up
    to `limit` rows)."""
    session = _session_or_404(tenant, session_id)
    return {"traces": session.read_trace(since=_since(since_minutes), limit=limit)}


# ── the session workflow ──

def _session_or_404(tenant: Tenant, session_id: str) -> SessionDir:
    try:
        session = _sessions.get(tenant.user, tenant.name, session_id)
    except ValueError:
        raise HTTPException(400, "Invalid session id")
    if session is None:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return session


def _ref(tenant: Tenant, session_id: str) -> SessionRef:
    return session_ref(tenant.user, tenant.name, session_id)


def _live_worker(tenant: Tenant, session_id: str):
    from app.routes import chat
    return chat.workers.get(session_key(tenant.user, tenant.name, session_id))


@router.get("/{session_id}")
async def get_session(session_id: str, tenant: Tenant = Depends(verify)):
    session = _session_or_404(tenant, session_id)
    meta = session.read_meta()
    return {"session_id": session_id, "updated_at": meta.get("updated_at"),
            "workflow": temporal.status_dict(await temporal.status(_ref(tenant, session_id)))}


@router.post("/{session_id}/abort")
async def abort_session(session_id: str, tenant: Tenant = Depends(verify)):
    """The `abort` signal (the workflow aborts the running or parked task)
    on the workflow path (503 while Temporal is not ready); on the direct
    path abort{aborted} to the live worker's guest."""
    _session_or_404(tenant, session_id)
    if temporal.configured():
        from app.routes import chat
        await chat.require_workflow_ready()
        try:
            await signal_abort(temporal.client, _ref(tenant, session_id), "aborted")
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(404, "No workflow for this session")
            raise
        return {"status": "ok", "signal": "abort"}
    w = _live_worker(tenant, session_id)
    if w is None or w.current_input_id is None or w.abort("aborted") == "no_guest":
        raise HTTPException(409, "No running task")
    return {"status": "ok", "abort": "sent"}


@router.get("/{session_id}/events")
def session_events(session_id: str, after_seq: int = 0, tenant: Tenant = Depends(verify)):
    """The session's guest events (current run) with seq > after_seq, for a
    client catching up after a disconnect. A plain def: FastAPI runs the
    file reads in its threadpool, off the event loop."""
    session = _session_or_404(tenant, session_id)
    w = _live_worker(tenant, session_id)
    after_seq = max(0, after_seq)
    if w is not None:
        return w.sync.read(after_seq)       # bounded by the acked seq, thread-safe
    return read_current_run(session.events, after_seq)
