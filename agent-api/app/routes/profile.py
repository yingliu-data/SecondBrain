from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.middleware import verify
from app.session.ids import current_user_id
from app.session.session_dir import utcnow_iso
from app.user.profile import UserProfile

router = APIRouter(prefix="/api/v1/profile", dependencies=[Depends(verify)])


class ProfilePatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=200)
    tone: str | None = Field(default=None, max_length=64)
    extras_md: str | None = Field(default=None, max_length=4000)


@router.get("")
def get_profile():
    p = UserProfile.load(current_user_id())
    return {
        "user_id": p.user_id,
        "display_name": p.display_name,
        "timezone": p.timezone,
        "email": p.email,
        "tone": p.tone,
        "extras_md": p.extras_md,
        "updated_at": p.updated_at,
    }


@router.patch("")
def patch_profile(patch: ProfilePatch):
    p = UserProfile.load(current_user_id())
    for k, v in patch.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    p.updated_at = utcnow_iso()
    p.save()
    return {"status": "ok", "updated_at": p.updated_at}
