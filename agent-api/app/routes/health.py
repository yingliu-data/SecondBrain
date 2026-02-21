from fastapi import APIRouter

router = APIRouter()
_llm = None


def set_llm(llm):
    global _llm
    _llm = llm


@router.get("/health")
async def health():
    return await _llm.health()
