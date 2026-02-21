from fastapi import APIRouter, Request, HTTPException, Depends
from app.auth.middleware import verify
from app.agent.loop import tool_result_events, tool_results

router = APIRouter()


@router.post("/api/v1/tool-result", dependencies=[Depends(verify)])
async def tool_result_endpoint(request: Request):
    body = await request.json()
    call_id = body.get("tool_call_id")
    result = body.get("result", "")[:2000]
    if call_id in tool_result_events:
        tool_results[call_id] = result
        tool_result_events[call_id].set()
        return {"status": "ok"}
    raise HTTPException(404, detail="Unknown tool call ID. It may have already timed out.")
