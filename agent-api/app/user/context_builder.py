"""ContextBuilder — composes the system prompt from base template + profile
+ memory index + selectively recalled memory bodies for one agent turn.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.config import SYSTEM_PROMPT, USERS_ROOT
from app.user.memory import MemoryStore
from app.user.profile import UserProfile

# Cap on inline memory bodies to keep the prompt budget predictable.
_MAX_RECALLED_BODIES = 3


class ContextBuilder:
    def __init__(self, *, user_id: str, session) -> None:
        self.user_id = user_id
        self.session = session
        self.profile = UserProfile.load(user_id)
        self.user_memory = MemoryStore(Path(USERS_ROOT) / user_id / "memory")
        self.session_memory = MemoryStore(session.memory)

    def build_system(self, message: str) -> str:
        base = SYSTEM_PROMPT.format(current_time=datetime.now().isoformat())
        parts: list[str] = [base]

        profile_block = self.profile.to_prompt_block()
        if profile_block:
            parts.extend(["", profile_block])

        user_idx = self._read_if_nonempty(self.user_memory.index)
        if user_idx:
            parts.extend(["", "USER MEMORY INDEX:", user_idx])
        session_idx = self._read_if_nonempty(self.session_memory.index)
        if session_idx:
            parts.extend(["", "SESSION MEMORY INDEX:", session_idx])

        recalled = (
            self.user_memory.recall(message)
            + self.session_memory.recall(message)
        )
        for r in recalled[:_MAX_RECALLED_BODIES]:
            parts.extend(
                ["", f"[memory:{r.type}:{r.slug}] {r.name}", r.body.strip()]
            )

        return "\n".join(parts)

    @staticmethod
    def _read_if_nonempty(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""
        return text.strip()
