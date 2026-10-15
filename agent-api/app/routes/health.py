from fastapi import APIRouter

from app.skills.avatar_control.planner_llm import planner_llm

router = APIRouter()


@router.get("/health")
async def health():
    """agent-api is up; `local` is the LLM server's own /health."""
    try:
        return {"status": "ok", "local": await planner_llm.health()}
    except Exception as e:
        return {"status": "degraded", "local": f"not_running: {e}"}
