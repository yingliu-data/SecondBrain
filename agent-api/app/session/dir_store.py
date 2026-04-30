"""DirStore — filesystem-backed session store. Replaces SQLite SessionStore."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.session.ids import safe_key
from app.session.session_dir import SessionDir


class DirStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, user_id: str, session_id: str) -> str:
        return safe_key(user_id, session_id)

    def path_for(self, user_id: str, session_id: str) -> Path:
        return self.root / self._key(user_id, session_id)

    def get(self, user_id: str, session_id: str) -> SessionDir | None:
        p = self.path_for(user_id, session_id)
        if not (p / "session.json").exists():
            return None
        return SessionDir.load(p)

    def get_or_create(self, user_id: str, session_id: str) -> SessionDir:
        existing = self.get(user_id, session_id)
        if existing is not None:
            return existing
        return SessionDir.create(
            self.path_for(user_id, session_id),
            user_id=user_id,
            session_id=session_id,
        )

    def delete(self, user_id: str, session_id: str) -> bool:
        p = self.path_for(user_id, session_id)
        if not p.exists():
            return False
        shutil.rmtree(p)
        return True

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        prefix = user_id + "_"
        out: list[dict[str, Any]] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or not entry.name.startswith(prefix):
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
                "updated_at": meta.get("updated_at"),
                "state": meta.get("state"),
                "title": meta.get("title", ""),
                "message_count": meta.get("message_count", 0),
            })
        out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return out
