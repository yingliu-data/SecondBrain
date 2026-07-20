from fastapi import APIRouter, HTTPException, Depends
from app.auth.middleware import verify
from app.tenants import Tenant

router = APIRouter(prefix="/api/v1/sessions")

_sessions = None


def set_sessions(sessions):
    global _sessions
    _sessions = sessions


def _visible_to(tenant: Tenant, store_key: str) -> bool:
    """Default tenant owns un-namespaced keys; others own their prefix."""
    if tenant.is_default:
        return ":" not in store_key
    return store_key.startswith(f"{tenant.name}:")


def _strip_prefix(tenant: Tenant, store_key: str) -> str:
    if tenant.is_default:
        return store_key
    return store_key[len(tenant.name) + 1:]


@router.get("")
async def list_sessions(tenant: Tenant = Depends(verify)):
    if hasattr(_sessions, "list_sessions"):
        rows = _sessions.list_sessions()
        out = []
        for row in rows:
            if _visible_to(tenant, row["session_id"]):
                out.append({**row, "session_id": _strip_prefix(tenant, row["session_id"])})
        return {"sessions": out}
    return {"sessions": [
        {"session_id": _strip_prefix(tenant, k)}
        for k in _sessions.keys() if _visible_to(tenant, k)
    ]}


@router.delete("/{session_id}")
async def delete_session(session_id: str, tenant: Tenant = Depends(verify)):
    store_key = tenant.session_key(session_id)
    if hasattr(_sessions, "delete"):
        if _sessions.delete(store_key):
            return {"status": "ok", "deleted": session_id}
    elif store_key in _sessions:
        del _sessions[store_key]
        return {"status": "ok", "deleted": session_id}
    raise HTTPException(404, f"Session '{session_id}' not found")
