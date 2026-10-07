from fastapi import APIRouter, Request, HTTPException, Depends
from app.auth.middleware import verify
from app.agent.loop import tool_result_events, tool_results
from app.host.sides import DEVICE_CALLS
from app.routes.chat import uses_harness
from app.tenants import Tenant

router = APIRouter()


@router.post("/api/v1/tool-result")
async def tool_result_endpoint(request: Request, tenant: Tenant = Depends(verify)):
    body = await request.json()
    call_id = body.get("tool_call_id")
    result = body.get("result", "")[:2000]
    if uses_harness(tenant):
        # Harness engine: the open call lives in DeviceCalls, not in the
        # stream, so it survives a dropped SSE connection. Unknown or
        # terminal call ids are 409 (PLAN.md 14).
        if DEVICE_CALLS.resolve(call_id, result):
            return {"status": "ok"}
        raise HTTPException(409, detail="Unknown or already completed tool call ID.")
    if call_id in tool_result_events:
        tool_results[call_id] = result
        tool_result_events[call_id].set()
        return {"status": "ok"}
    raise HTTPException(404, detail="Unknown tool call ID. It may have already timed out.")
