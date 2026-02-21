from fastapi import APIRouter, HTTPException, Depends
from app.auth.middleware import verify

router = APIRouter(prefix="/api/v1/skills", dependencies=[Depends(verify)])

# registry is injected at startup (see main.py)
_registry = None


def set_registry(registry):
    global _registry
    _registry = registry


@router.get("")
async def list_skills():
    """List all available skills with metadata and enabled state."""
    return {"skills": _registry.list_all()}


@router.patch("/{skill_name}")
async def update_skill(skill_name: str, body: dict):
    """Enable or disable a skill. Body: {"enabled": true/false}"""
    if "enabled" not in body:
        raise HTTPException(400, "Body must include 'enabled' field")
    if not _registry.set_enabled(skill_name, body["enabled"]):
        raise HTTPException(404, f"Skill '{skill_name}' not found. Use GET /api/v1/skills to see available skills.")
    return {"status": "ok", "skill": skill_name, "enabled": body["enabled"]}


@router.get("/{skill_name}")
async def get_skill(skill_name: str):
    """Get details for a specific skill."""
    for s in _registry.list_all():
        if s["name"] == skill_name:
            return s
    raise HTTPException(404, f"Skill '{skill_name}' not found.")
