"""DirStore — filesystem-backed session store. Replaces SQLite SessionStore.

Layout: ``{root}/{user}/{tenant}_{session_id}/`` — sessions are separated
per tenant (different toolsets must not mix) but grouped under the user who
owns those tenants.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.session.ids import is_safe_id, safe_key
from app.session.session_dir import SessionDir


class DirStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, user: str, tenant: str, session_id: str) -> Path:
        if not is_safe_id(user):
            raise ValueError(f"user fails safe-path validation: {user!r}")
        return self.root / user / safe_key(tenant, session_id)

    def get(self, user: str, tenant: str, session_id: str) -> SessionDir | None:
        p = self.path_for(user, tenant, session_id)
        if not (p / "session.json").exists():
            return None
        return SessionDir.load(p)

    def get_or_create(self, user: str, tenant: str, session_id: str) -> SessionDir:
        existing = self.get(user, tenant, session_id)
        if existing is not None:
            return existing
        return SessionDir.create(
            self.path_for(user, tenant, session_id),
            user_id=user,
            session_id=session_id,
            tenant=tenant,
        )

    def delete(self, user: str, tenant: str, session_id: str) -> bool:
        p = self.path_for(user, tenant, session_id)
        if not p.exists():
            return False
        shutil.rmtree(p)
        return True

    def list_for_tenant(self, user: str, tenant: str) -> list[dict[str, Any]]:
        """Sessions belonging to one tenant of one user, newest first."""
        return self._scan(user, prefix=tenant + "_")

    def list_for_user(self, user: str) -> list[dict[str, Any]]:
        """All sessions across a user's tenants, newest first."""
        return self._scan(user, prefix=None)

    def _scan(self, user: str, *, prefix: str | None) -> list[dict[str, Any]]:
        user_dir = self.root / user
        if not is_safe_id(user) or not user_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for entry in sorted(user_dir.iterdir()):
            if not entry.is_dir():
                continue
            if prefix is not None and not entry.name.startswith(prefix):
                continue
            meta_file = entry / "session.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text())
            except json.JSONDecodeError:
                continue
            out.append({
                "session_id": meta.get("session_id"),
                "tenant": meta.get("tenant", ""),
                "updated_at": meta.get("updated_at"),
                "state": meta.get("state"),
                "title": meta.get("title", ""),
                "message_count": meta.get("message_count", 0),
            })
        out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return out
