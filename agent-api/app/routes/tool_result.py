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
    are 409 (PLAN.md 14)."""
    body = await request.json()
    call_id = body.get("tool_call_id")
    result = body.get("result", "")[:2000]
    if DEVICE_CALLS.resolve(call_id, result,
                            owner_prefix=session_key(tenant.user, tenant.name, "")):
        return {"status": "ok"}
    raise HTTPException(409, detail="Unknown or already completed tool call ID.")
