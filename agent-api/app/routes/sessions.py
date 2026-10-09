import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends
from temporalio.service import RPCError, RPCStatusCode
from app.auth.middleware import verify
from flows.activities import SessionRef
from flows.client import signal_abort
from app.host import temporal
from app.host.state_sync import read_current_run
from app.session.dir_store import DirStore
from app.session.eventlog import parse_ts
from app.session.ids import session_key
from app.tenants import Tenant

router = APIRouter(prefix="/api/v1/sessions")
logger = logging.getLogger("routes.sessions")

_EPOCH = datetime.fromtimestamp(0, timezone.utc)

_sessions = None


def set_sessions(sessions):
    global _sessions
    _sessions = sessions


def _is_dir_store() -> bool:
    return isinstance(_sessions, DirStore)


# ── Legacy (sqlite/dict) helpers ─────────────────────────────

def _visible_to(tenant: Tenant, store_key: str) -> bool:
    if tenant.is_default:
        return ":" not in store_key
    return store_key.startswith(f"{tenant.name}:")


def _strip_prefix(tenant: Tenant, store_key: str) -> str:
    if tenant.is_default:
        return store_key
    return store_key[len(tenant.name) + 1:]


# ── Routes ───────────────────────────────────────────────────

@router.get("")
async def list_sessions(tenant: Tenant = Depends(verify)):
    if _is_dir_store():
        rows = _sessions.list_for_tenant(tenant.user, tenant.name)
        return {"sessions": [
            {"session_id": r["session_id"], "updated_at": r["updated_at"]}
            for r in rows
        ]}
    if hasattr(_sessions, "list_sessions"):
        rows = _sessions.list_sessions()
        return {"sessions": [
            {**row, "session_id": _strip_prefix(tenant, row["session_id"])}
            for row in rows if _visible_to(tenant, row["session_id"])
        ]}
    return {"sessions": [
        {"session_id": _strip_prefix(tenant, k)}
        for k in _sessions.keys() if _visible_to(tenant, k)
    ]}


@router.delete("/{session_id}")
async def delete_session(session_id: str, tenant: Tenant = Depends(verify)):
    if _is_dir_store():
        try:
            deleted = _sessions.delete(tenant.user, tenant.name, session_id)
        except ValueError:
            raise HTTPException(400, "Invalid session id")
        if deleted:
            return {"status": "ok", "deleted": session_id}
        raise HTTPException(404, f"Session '{session_id}' not found")
    store_key = tenant.store_key(session_id)
    if hasattr(_sessions, "delete"):
        if _sessions.delete(store_key):
            return {"status": "ok", "deleted": session_id}
    elif store_key in _sessions:
        del _sessions[store_key]
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
    tools ran in the last N minutes'. Built from each session's event log
    (every run; M6-CONTRACTS §2). Requires the dir backend. A plain def:
    FastAPI runs the file reads in its threadpool, off the event loop."""
    if not _is_dir_store():
        raise HTTPException(501, "Traces require SESSION_BACKEND=dir")
    floor = _since(since_minutes)
    out = []
    for row in _sessions.list_for_tenant(tenant.user, tenant.name):
        session = _sessions.get(tenant.user, tenant.name, row["session_id"])
        if session is None:
            continue
        try:
            rows = session.read_trace(since=floor)
        except Exception as e:      # one unreadable log never hides the others
            logger.warning("traces: session %s unreadable: %s", row["session_id"], e)
            continue
        for event in rows:
            out.append({"session_id": row["session_id"], **event})
    out.sort(key=lambda e: parse_ts(e.get("ts")) or _EPOCH)
    return {"traces": out[-limit:] if limit > 0 else []}


@router.get("/{session_id}/traces")
def session_traces(session_id: str, since_minutes: int | None = None,
                   limit: int = 200, tenant: Tenant = Depends(verify)):
    """One session's trace rows from its event log (every run)."""
    session = _session_or_404(tenant, session_id, "Traces require SESSION_BACKEND=dir")
    return {"traces": session.read_trace(since=_since(since_minutes), limit=limit)}


# ── M5: the session workflow (harness/docs/M5-CONTRACTS.md §3) ──

def _session_or_404(tenant: Tenant, session_id: str,
                    not_dir: str = "Requires SESSION_BACKEND=dir"):
    if not _is_dir_store():
        raise HTTPException(501, not_dir)
    try:
        session = _sessions.get(tenant.user, tenant.name, session_id)
    except ValueError:
        raise HTTPException(400, "Invalid session id")
    if session is None:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return session


def _ref(tenant: Tenant, session_id: str) -> SessionRef:
    return SessionRef(user=tenant.user, tenant=tenant.name, session_id=session_id)


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
    """The `abort` signal (the workflow aborts the running or parked task).
    Without Temporal: abort{aborted} to the live worker's guest."""
    _session_or_404(tenant, session_id)
    if temporal.enabled():
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
    return read_current_run(session.root / "events", after_seq)
