from fastapi import APIRouter, Request, HTTPException, Depends
from app.auth.middleware import verify
from app.host.sides import DEVICE_CALLS, DeliveryError
from app.tenants import Tenant

router = APIRouter()


@router.post("/api/v1/tool-result")
async def tool_result_endpoint(request: Request, tenant: Tenant = Depends(verify)):
    """A device tool's result. The open call lives in DeviceCalls, not in the
    stream, so it survives a dropped SSE connection. Unknown or terminal call
    ids — and calls of another tenant's sessions (the call's SessionRef
    user and tenant, compared exactly) — are 409. An id this process does not know may be open in the log of a
    parked session no Worker has loaded since an agent-api restart: the
    tenant's parked sessions are loaded (each Worker rebuilds its open
    device calls from its log) and the id is tried once more. A result that
    could not be handed to its session (DeliveryError) is a 503: the call
    stays open and the same POST may be retried."""
    from app.routes import chat
    body = await request.json()
    call_id = body.get("tool_call_id")
    result = body.get("result", "")[:2000]
    owner = (tenant.user, tenant.name)
    try:
        if DEVICE_CALLS.resolve(call_id, result, owner=owner):
            return {"status": "ok"}
        if await chat.workers.load_parked(tenant, chat._registry) and \
                DEVICE_CALLS.resolve(call_id, result, owner=owner):
            return {"status": "ok"}
    except DeliveryError as e:
        raise HTTPException(503, detail=f"{e}; retry", headers={"Retry-After": "1"})
    raise HTTPException(409, detail="Unknown or already completed tool call ID.")
