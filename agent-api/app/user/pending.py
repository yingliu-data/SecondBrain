"""PendingStore — profile-change proposals awaiting user approval.

Layout::

    {USERS_ROOT}/{user_id}/pending/
        {change_id}.json   # one pending change per file

Each change is one proposed ``UserProfile`` patch. Memory extractions are
NOT gated here — they go straight to the memory store because they're
auxiliary and auditable via ``MEMORY.md``. Profile patches gate here
because they shape identity / system prompt and auto-applying could
scramble the model's behaviour.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.session.session_dir import utcnow_iso
from app.util.ipc import atomic_write_json

_VALID_CHANGE_ID = re.compile(r"^pc_[a-z0-9]{8,32}$")

# Only these profile keys may be proposed. Anything else is dropped to
# guard against extractor hallucinations writing unrelated fields.
ALLOWED_PROFILE_FIELDS = {"display_name", "timezone", "email", "tone"}


def _make_change_id() -> str:
    return "pc_" + uuid.uuid4().hex[:12]


@dataclass
class PendingChange:
    id: str
    user_id: str
    proposed_by: str                       # "extractor" | "manual"
    created_at: str
    fields: dict[str, str] = field(default_factory=dict)
    rationale: str = ""                    # one-line explanation from extractor

    def to_dict(self) -> dict:
        return asdict(self)


class PendingStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, change_id: str) -> Path:
        if not _VALID_CHANGE_ID.match(change_id):
            raise ValueError(f"invalid change id: {change_id!r}")
        return self.root / f"{change_id}.json"

    def propose(
        self,
        *,
        user_id: str,
        fields: dict[str, str],
        proposed_by: str = "extractor",
        rationale: str = "",
    ) -> PendingChange | None:
        """Queue a proposed profile patch. Returns None if the patch is empty
        after filtering against ``ALLOWED_PROFILE_FIELDS``."""
        clean: dict[str, str] = {}
        for k, v in (fields or {}).items():
            if k in ALLOWED_PROFILE_FIELDS and isinstance(v, str) and v.strip():
                clean[k] = v.strip()[:200]
        if not clean:
            return None
        change = PendingChange(
            id=_make_change_id(),
            user_id=user_id,
            proposed_by=proposed_by,
            created_at=utcnow_iso(),
            fields=clean,
            rationale=rationale[:500] if rationale else "",
        )
        atomic_write_json(self._path(change.id), change.to_dict())
        return change

    def list(self) -> list[PendingChange]:
        out: list[PendingChange] = []
        for p in sorted(self.root.glob("pc_*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                out.append(PendingChange(**raw))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return out

    def get(self, change_id: str) -> PendingChange | None:
        try:
            path = self._path(change_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return PendingChange(**raw)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def remove(self, change_id: str) -> bool:
        try:
            path = self._path(change_id)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True
