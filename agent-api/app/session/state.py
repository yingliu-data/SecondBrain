"""Forward-only state machine for a session.

"State is forward-only. Retries are new sessions." — PROJECT_STRUCTURE.md.
"""
from __future__ import annotations


class StateError(ValueError):
    """Raised on an illegal state transition."""


ALLOWED: dict[str, set[str]] = {
    "active": {"complete", "failed", "escalated"},
    "complete": set(),
    "failed": set(),
    "escalated": set(),
}


def transition(current: str, new: str) -> str:
    """Return ``new`` if the transition is permitted, else raise ``StateError``.

    Identity transitions (``current == new``) are a no-op and always allowed.
    """
    if current == new:
        return new
    allowed = ALLOWED.get(current)
    if allowed is None:
        raise StateError(f"Unknown state: {current!r}")
    if new not in allowed:
        raise StateError(f"Cannot transition {current!r} -> {new!r}")
    return new
