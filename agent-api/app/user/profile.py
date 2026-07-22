"""UserProfile — per-user durable facts loaded into the system prompt.

Storage layout::

    {USERS_ROOT}/{user_id}/
        profile.json    # structured fields
        profile.md      # free-form extras (capped to keep prompt budget tight)
        memory/         # user-tier memory files (see app/user/memory.py)
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import USERS_ROOT

# Profile sits in the system prompt on every turn — keep it cheap.
_PROMPT_MAX_CHARS = 1200

_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _user_dir(user_id: str) -> Path:
    if not _SAFE_USER_RE.match(user_id):
        raise ValueError(f"user_id fails safe-path validation: {user_id!r}")
    return Path(USERS_ROOT) / user_id


@dataclass
class UserProfile:
    user_id: str
    display_name: str = ""
    timezone: str = "UTC"
    email: str = ""
    tone: str = "concise"
    extras_md: str = ""
    updated_at: str = ""

    @property
    def dir(self) -> Path:
        return _user_dir(self.user_id)

    @classmethod
    def load(cls, user_id: str) -> "UserProfile":
        base = _user_dir(user_id)
        data: dict = {"user_id": user_id}
        pj = base / "profile.json"
        if pj.exists():
            try:
                raw = json.loads(pj.read_text(encoding="utf-8"))
                for k in ("display_name", "timezone", "email", "tone", "updated_at"):
                    if k in raw and isinstance(raw[k], str):
                        data[k] = raw[k]
            except json.JSONDecodeError:
                pass
        pm = base / "profile.md"
        if pm.exists():
            data["extras_md"] = pm.read_text(encoding="utf-8")
        return cls(**data)

    def save(self) -> None:
        base = _user_dir(self.user_id)
        base.mkdir(parents=True, exist_ok=True)
        structured = asdict(self)
        extras = structured.pop("extras_md", "") or ""
        (base / "profile.json").write_text(
            json.dumps(structured, indent=2), encoding="utf-8"
        )
        (base / "profile.md").write_text(extras, encoding="utf-8")

    def to_prompt_block(self, max_chars: int = _PROMPT_MAX_CHARS) -> str:
        lines: list[str] = []
        if self.display_name:
            lines.append(f"- name: {self.display_name}")
        if self.timezone:
            lines.append(f"- timezone: {self.timezone}")
        if self.email:
            lines.append(f"- email: {self.email}")
        if self.tone:
            lines.append(f"- tone: {self.tone}")
        extras = (self.extras_md or "").strip()
        if not lines and not extras:
            return ""
        parts = ["USER PROFILE:"]
        if lines:
            parts.extend(lines)
        if extras:
            parts.extend(["", extras])
        block = "\n".join(parts)
        return block if len(block) <= max_chars else block[: max_chars - 1] + "…"
