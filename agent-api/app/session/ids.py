"""Session/ticket identifier generation and path-safe key validation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

# one rule for user/session ids on both sides of the gateway (protocol.mcp)
from protocol.mcp import is_safe_id


def make_session_id() -> str:
    """Traceable session id: UTC date-time plus a short random suffix,
    e.g. "sess_2026-07-23_140905_a1b2"."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return f"sess_{ts}_{uuid.uuid4().hex[:4]}"


def make_ticket_id() -> str:
    return "tk_" + uuid.uuid4().hex[:8]


def safe_key(user_id: str, session_id: str) -> str:
    """Compose a filesystem-safe directory name from user_id + session_id.

    Both components must match ``^[A-Za-z0-9_\\-]{1,64}$``. The filesystem key
    is the literal ``f"{user_id}_{session_id}"``.
    """
    if not is_safe_id(user_id):
        raise ValueError(f"user_id fails safe-path validation: {user_id!r}")
    if not is_safe_id(session_id):
        raise ValueError(f"session_id fails safe-path validation: {session_id!r}")
    return f"{user_id}_{session_id}"
