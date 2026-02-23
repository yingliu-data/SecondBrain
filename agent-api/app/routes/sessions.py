from fastapi import APIRouter, HTTPException, Depends
from app.auth.middleware import verify

router = APIRouter(prefix="/api/v1/sessions", dependencies=[Depends(verify)])

_sessions = None


def set_sessions(sessions):
    global _sessions
    _sessions = sessions


@router.get("")
async def list_sessions():
    if hasattr(_sessions, "list_sessions"):
        return {"sessions": _sessions.list_sessions()}
    return {"sessions": [{"session_id": k} for k in _sessions.keys()]}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    if hasattr(_sessions, "delete"):
        if _sessions.delete(session_id):
            return {"status": "ok", "deleted": session_id}
    elif session_id in _sessions:
        del _sessions[session_id]
        return {"status": "ok", "deleted": session_id}
    raise HTTPException(404, f"Session '{session_id}' not found")
