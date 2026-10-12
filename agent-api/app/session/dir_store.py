"""DirStore — the filesystem-backed session store.

Layout: ``{root}/{user}/{tenant}.{session_id}/`` — sessions are separated
per tenant (different toolsets must not mix) but grouped under the user who
owns those tenants. The directory name is injective: user, tenant and
session ids are safe ids (``[A-Za-z0-9_-]``), so ``.`` cannot occur in
either half.

A directory counts as a session only when its name is exactly
``{tenant}.{session_id}`` of its own ``session.json`` (user, tenant,
session_id compared exactly): a copied or hand-made directory is never
some other session. Every lookup and listing compares that parsed
identity, never a name prefix.

The old layout ``{user}/{tenant}_{session_id}/`` is not supported (it was
ambiguous across tenants whose names contain ``_``). Such directories are
ignored — never opened, listed or collided with; `legacy_layout_dirs`
finds them, agent-api logs them once at startup, and
``python -m app.session.migrate --purge-legacy-layout`` deletes them.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.session.ids import is_safe_id, session_dir_name
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
        """The directory of the session, `{user}/{tenant}.{session_id}`
        (whether or not it exists yet)."""
        return self.canonical_path(user, tenant, session_id)

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

    def list_for_tenant(self, user: str, tenant: str, *,
                        require: str | None = None) -> list[dict[str, Any]]:
        """Sessions belonging to one tenant of one user (session.json's
        tenant, compared exactly), newest first:
        {session_id, tenant, updated_at, state, title, root}. Only
        directories named `{tenant}.*` have their session.json read; with `require` (a path
        relative to the session directory, e.g. "vm/parked.json") only
        those that also hold it. Blocking I/O: call it off the loop."""
        return self._scan(user, tenant=tenant, require=require)

    def list_for_user(self, user: str) -> list[dict[str, Any]]:
        """All sessions across a user's tenants, newest first."""
        return self._scan(user, tenant=None)

    def _scan(self, user: str, *, tenant: str | None,
              require: str | None = None) -> list[dict[str, Any]]:
        if not is_safe_id(user):
            return []
        user_dir = self.root / user
        if not user_dir.is_dir():
            return []
        # a cheap name filter before any session.json is read; the identity
        # below still decides
        prefix = f"{tenant}." if tenant is not None else None
        out: list[dict[str, Any]] = []
        for entry in sorted(user_dir.iterdir()):
            if prefix is not None and not entry.name.startswith(prefix):
                continue
            if require is not None and not (entry / require).exists():
                continue
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
            if entry.name != session_dir_name(t, sid):
                continue
            meta = ident["meta"]
            out.append({
                "session_id": sid,
                "tenant": t,
                "updated_at": meta.get("updated_at"),
                "state": meta.get("state"),
                "title": meta.get("title", ""),
                "root": entry,
            })
        out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return out


# ---------------------------------------------------------------- old layout
def legacy_layout_dirs(root: Path | str) -> list[Path]:
    """Session directories of the unsupported old layout
    `{user}/{tenant}_{session_id}` under `root`, sorted. Conservative: a
    directory (not a symlink) directly under a user directory (not a
    symlink) whose name holds no `.` and which holds a `session.json` file.
    A new-layout name always holds the `.` (safe ids never do), so no
    current session directory -- valid, copied or hand-made -- matches;
    nor does anything without a session.json. Blocking I/O."""
    root = Path(root)
    found: list[Path] = []
    try:
        users = sorted(root.iterdir())
    except OSError:
        return []
    for user_dir in users:
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        try:
            entries = sorted(user_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if "." in entry.name or entry.is_symlink() or not entry.is_dir():
                continue
            sj = entry / "session.json"
            if sj.is_file() and not sj.is_symlink():
                found.append(entry)
    return found


def warn_legacy_layout(root: Path | str, logger: logging.Logger | None = None) -> list[Path]:
    """Log one WARNING naming every old-layout session directory under
    `root` (they are ignored; nothing is deleted). Returns them."""
    found = legacy_layout_dirs(root)
    if found:
        (logger or logging.getLogger(__name__)).warning(
            "%d session director%s in the old {tenant}_{session_id} layout ignored; "
            "remove with `python -m app.session.migrate --purge-legacy-layout "
            "[--dry-run] %s`: %s",
            len(found), "y" if len(found) == 1 else "ies", root,
            ", ".join(str(p.relative_to(root)) for p in found))
    return found
