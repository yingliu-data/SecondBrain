from app.config import SESSION_BACKEND, SESSIONS_ROOT
from app.session.dir_store import DirStore


def create_session_store() -> DirStore:
    """The directory session store, the only one the harness engine runs on.
    Any other SESSION_BACKEND stops agent-api at startup."""
    if SESSION_BACKEND != "dir":
        raise RuntimeError(
            f"SESSION_BACKEND={SESSION_BACKEND!r} is not supported: the harness engine "
            f"needs the directory session store (SESSION_BACKEND=dir, the default). "
            f"The legacy engine that ran on sqlite/memory was removed; migrate "
            f"old sqlite sessions with app/session/migrate.py.")
    return DirStore(SESSIONS_ROOT)
