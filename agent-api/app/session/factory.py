from app.config import SESSION_BACKEND, SESSION_DB_PATH


def create_session_store():
    """Return the right store based on SESSION_BACKEND config."""
    if SESSION_BACKEND == "sqlite":
        from app.session.store import SessionStore

        return SessionStore(SESSION_DB_PATH)
    # "memory" — plain dict, same as original behavior
    return {}
