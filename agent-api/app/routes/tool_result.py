from fastapi import APIRouter, Request, HTTPException, Depends
from app.auth.middleware import verify
from app.host.sides import DEVICE_CALLS
from app.session.ids import session_key
from app.tenants import Tenant

router = APIRouter()


@router.post("/api/v1/tool-result")
async def tool_result_endpoint(request: Request, tenant: Tenant = Depends(verify)):
    """A device tool's result. The open call lives in DeviceCalls, not in the
    stream, so it survives a dropped SSE connection. Unknown or terminal call
    ids — and calls of another tenant's sessions (the session_key prefix) —
    are 409. An id this process does not know may be open in the log of a
    parked session no Worker has loaded since an agent-api restart: the
    tenant's parked sessions are loaded (each Worker rebuilds its open
    device calls from its log) and the id is tried once more."""
    from app.routes import chat
    body = await request.json()
    call_id = body.get("tool_call_id")
    result = body.get("result", "")[:2000]
    owner = session_key(tenant.user, tenant.name, "")
    if DEVICE_CALLS.resolve(call_id, result, owner_prefix=owner):
        return {"status": "ok"}
    if chat.workers.load_parked(tenant, chat._registry) and \
            DEVICE_CALLS.resolve(call_id, result, owner_prefix=owner):
        return {"status": "ok"}
    raise HTTPException(409, detail="Unknown or already completed tool call ID.")
