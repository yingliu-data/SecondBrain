from app.config import SESSION_BACKEND, SESSION_DB_PATH, SESSIONS_ROOT


def create_session_store():
    """Return the right store based on SESSION_BACKEND config.

    "dir" (default) — DirStore: one directory per session, tickets/traces/memory.
    "sqlite"        — legacy SessionStore blob (kept as fallback).
    "memory"        — plain dict, tests only.
    """
    if SESSION_BACKEND == "dir":
        from app.session.dir_store import DirStore

        return DirStore(SESSIONS_ROOT)
    if SESSION_BACKEND == "sqlite":
        from app.session.store import SessionStore

        return SessionStore(SESSION_DB_PATH)
    return {}
