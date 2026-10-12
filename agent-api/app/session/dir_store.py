"""DirStore — the filesystem-backed session store.

Layout: ``{root}/{user}/{tenant}.{session_id}/`` — sessions are separated
per tenant (different toolsets must not mix) but grouped under the user who
owns those tenants. The directory name is injective: user, tenant and
session ids are safe ids (``[A-Za-z0-9_-]``), so ``.`` cannot occur in
either half.

Legacy layout: ``{root}/{user}/{tenant}_{session_id}/`` (before the
``.`` separator). It is ambiguous — tenant ``wcc`` + session ``event_x``
and tenant ``wcc_event`` + session ``x`` share one name — so a legacy
directory is only ever taken for the session its ``session.json`` names
exactly (user, tenant, session_id); it stays readable in place, and
``python -m app.session.migrate --layout`` renames legacy directories to
the new layout. Every lookup and listing compares the parsed identity
(``session.json``), never a name prefix.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.session.ids import is_safe_id, session_dir_name, legacy_dir_name
from app.session.session_dir import SessionDir


def read_identity(path: Path) -> dict[str, Any] | None:
    """{user, tenant, session_id, meta} of a session directory, from its
    session.json; None when it has none or it is unreadable."""
    try:
        meta = json.loads((path / "session.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    return {"user": meta.get("user_id"), "tenant": meta.get("tenant"),
            "session_id": meta.get("session_id"), "meta": meta}


def _is_session(path: Path, user: str, tenant: str, session_id: str) -> bool:
    """`path` holds exactly this session (session.json identity, compared
    exactly; a missing user_id counts as the directory's user)."""
    ident = read_identity(path)
    if ident is None:
        return False
    return (ident["tenant"] == tenant and ident["session_id"] == session_id
            and ident["user"] in (None, user))


class DirStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def canonical_path(self, user: str, tenant: str, session_id: str) -> Path:
        """Where a new session of (user, tenant, session_id) is created."""
        if not is_safe_id(user):
            raise ValueError(f"user fails safe-path validation: {user!r}")
        return self.root / user / session_dir_name(tenant, session_id)

    def path_for(self, user: str, tenant: str, session_id: str) -> Path:
        """The directory of the session: the canonical one, or a legacy
        `{tenant}_{session_id}` directory whose session.json names exactly
        this session; the canonical path when neither exists yet."""
        canonical = self.canonical_path(user, tenant, session_id)
        if (canonical / "session.json").exists():
            return canonical
        legacy = self.root / user / legacy_dir_name(tenant, session_id)
        if _is_session(legacy, user, tenant, session_id):
            return legacy
        return canonical

    def get(self, user: str, tenant: str, session_id: str) -> SessionDir | None:
        p = self.path_for(user, tenant, session_id)
        if not _is_session(p, user, tenant, session_id):
            return None
        return SessionDir.load(p)

    def get_or_create(self, user: str, tenant: str, session_id: str) -> SessionDir:
        existing = self.get(user, tenant, session_id)
        if existing is not None:
            return existing
        p = self.canonical_path(user, tenant, session_id)
        if (p / "session.json").exists():
            # the canonical name is injective: only a hand-made directory
            # could hold another identity here
            raise ValueError(f"session directory {p} belongs to another session")
        return SessionDir.create(p, user_id=user, session_id=session_id, tenant=tenant)

    def delete(self, user: str, tenant: str, session_id: str) -> bool:
        session = self.get(user, tenant, session_id)
        if session is None:
            return False
        shutil.rmtree(session.root)
        return True

    def list_for_tenant(self, user: str, tenant: str) -> list[dict[str, Any]]:
        """Sessions belonging to one tenant of one user (session.json's
        tenant, compared exactly), newest first:
        {session_id, tenant, updated_at, state, title, root}."""
        return self._scan(user, tenant=tenant)

    def list_for_user(self, user: str) -> list[dict[str, Any]]:
        """All sessions across a user's tenants, newest first."""
        return self._scan(user, tenant=None)

    def _scan(self, user: str, *, tenant: str | None) -> list[dict[str, Any]]:
        if not is_safe_id(user):
            return []
        user_dir = self.root / user
        if not user_dir.is_dir():
            return []
        found: dict[tuple, dict[str, Any]] = {}
        for entry in sorted(user_dir.iterdir()):
            if not entry.is_dir():
                continue
            ident = read_identity(entry)
            if ident is None:
                continue
            t, sid = ident["tenant"], ident["session_id"]
            if not (isinstance(t, str) and isinstance(sid, str)
                    and is_safe_id(t) and is_safe_id(sid)):
                continue
            if ident["user"] not in (None, user):
                continue
            if tenant is not None and t != tenant:
                continue
            # only a directory named for its own identity counts (a copied
            # or hand-made directory is not some other session)
            canonical = entry.name == session_dir_name(t, sid)
            if not canonical and entry.name != legacy_dir_name(t, sid):
                continue
            key = (t, sid)
            if key in found and not canonical:
                continue                    # the canonical directory wins
            meta = ident["meta"]
            found[key] = {
                "session_id": sid,
                "tenant": t,
                "updated_at": meta.get("updated_at"),
                "state": meta.get("state"),
                "title": meta.get("title", ""),
                "root": entry,
            }
        out = list(found.values())
        out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return out
