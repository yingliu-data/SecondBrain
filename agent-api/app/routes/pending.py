"""/api/v1/pending — approve/reject extractor-proposed profile patches."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.auth.middleware import verify
from app.config import USERS_ROOT
from app.session.ids import current_user_id
from app.session.session_dir import utcnow_iso
from app.user.pending import PendingStore
from app.user.profile import UserProfile

router = APIRouter(prefix="/api/v1/pending", dependencies=[Depends(verify)])


def _store_for(user_id: str) -> PendingStore:
    return PendingStore(Path(USERS_ROOT) / user_id / "pending")


@router.get("")
def list_pending():
    store = _store_for(current_user_id())
    return {"pending": [c.to_dict() for c in store.list()]}


@router.post("/{change_id}/approve")
def approve(change_id: str):
    user_id = current_user_id()
    store = _store_for(user_id)
    change = store.get(change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="pending change not found")

    profile = UserProfile.load(user_id)
    for k, v in change.fields.items():
        setattr(profile, k, v)
    profile.updated_at = utcnow_iso()
    profile.save()
    store.remove(change_id)
    return {"status": "approved", "applied": change.fields, "updated_at": profile.updated_at}


@router.post("/{change_id}/reject")
def reject(change_id: str):
    store = _store_for(current_user_id())
    if store.get(change_id) is None:
        raise HTTPException(status_code=404, detail="pending change not found")
    store.remove(change_id)
    return {"status": "rejected"}
