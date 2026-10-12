"""Session/ticket identifier generation and path-safe key validation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

# one rule for user/session ids on both sides of the gateway (protocol.mcp)
from protocol.mcp import is_safe_id
from flows.activities import SessionRef

__all__ = ["SessionRef", "is_safe_id", "legacy_dir_name", "make_session_id",
           "session_dir_name", "session_key", "session_ref"]


def make_session_id() -> str:
    """Traceable session id: UTC date-time plus a short random suffix,
    e.g. "sess_2026-07-23_140905_a1b2"."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return f"sess_{ts}_{uuid.uuid4().hex[:4]}"


def session_key(user: str, tenant: str, session_id: str) -> str:
    """The in-process key of one session (the Worker registry): injective,
    `/` occurs in none of the safe ids. Ownership checks never parse it:
    they compare the session's (user, tenant) exactly (SessionRef)."""
    return f"{user}/{tenant}/{session_id}"


def session_ref(user: str, tenant: str, session_id: str) -> SessionRef:
    """The identity of one session: (user, tenant, session_id)."""
    return SessionRef(user=user, tenant=tenant, session_id=session_id)


def session_dir_name(tenant: str, session_id: str) -> str:
    """A session's directory name under its user: ``{tenant}.{session_id}``.
    Injective: both halves must be safe ids, which never contain ``.``."""
    if not is_safe_id(tenant):
        raise ValueError(f"tenant fails safe-path validation: {tenant!r}")
    if not is_safe_id(session_id):
        raise ValueError(f"session_id fails safe-path validation: {session_id!r}")
    return f"{tenant}.{session_id}"


def legacy_dir_name(tenant: str, session_id: str) -> str:
    """The pre-``.`` directory name ``{tenant}_{session_id}``: ambiguous
    (tenant ``a`` + session ``b_c`` == tenant ``a_b`` + session ``c``), so
    only ever matched together with the directory's session.json."""
    return f"{tenant}_{session_id}"
