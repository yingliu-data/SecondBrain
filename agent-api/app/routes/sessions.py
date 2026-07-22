from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends
from app.auth.middleware import verify
from app.session.dir_store import DirStore
from app.tenants import Tenant

router = APIRouter(prefix="/api/v1/sessions")

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
    store_key = tenant.session_key(session_id)
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
async def recent_traces(since_minutes: int = 10, limit: int = 200,
                        tenant: Tenant = Depends(verify)):
    """Trace events across all of this tenant's sessions — answers
    'which tools ran in the last N minutes'. Requires the dir backend."""
    if not _is_dir_store():
        raise HTTPException(501, "Traces require SESSION_BACKEND=dir")
    floor = _since(since_minutes)
    out = []
    for row in _sessions.list_for_tenant(tenant.user, tenant.name):
        session = _sessions.get(tenant.user, tenant.name, row["session_id"])
        if session is None:
            continue
        for event in session.read_trace(since=floor):
            out.append({"session_id": row["session_id"], **event})
    out.sort(key=lambda e: e.get("ts", ""))
    return {"traces": out[-limit:]}


@router.get("/{session_id}/traces")
async def session_traces(session_id: str, since_minutes: int | None = None,
                         limit: int = 200, tenant: Tenant = Depends(verify)):
    if not _is_dir_store():
        raise HTTPException(501, "Traces require SESSION_BACKEND=dir")
    try:
        session = _sessions.get(tenant.user, tenant.name, session_id)
    except ValueError:
        raise HTTPException(400, "Invalid session id")
    if session is None:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return {"traces": session.read_trace(since=_since(since_minutes), limit=limit)}
