import httpx
from fastapi import APIRouter

from app.config import LLM_URL

router = APIRouter()


@router.get("/health")
async def health():
    """agent-api is up; `local` is the LLM server's own /health."""
    try:
        async with httpx.AsyncClient(base_url=LLM_URL, timeout=5) as client:
            r = await client.get("/health")
        local = (r.json() if r.headers.get("content-type", "").startswith("application/json")
                 else "ok")
        return {"status": "ok", "local": local}
    except Exception as e:
        return {"status": "degraded", "local": f"not_running: {e}"}
