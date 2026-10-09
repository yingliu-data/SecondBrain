from app.session.store import SessionStore


def test_store_isolation_between_tenants(tmp_path):
    store = SessionStore(str(tmp_path / "conv.db"))
    store.save("abc", [{"role": "user", "content": "default tenant"}])
    store.save("wcc-events:abc", [{"role": "user", "content": "wcc tenant"}])
    assert store.get("abc")[0]["content"] == "default tenant"
    assert store.get("wcc-events:abc")[0]["content"] == "wcc tenant"
    ids = {row["session_id"] for row in store.list_sessions()}
    assert ids == {"abc", "wcc-events:abc"}
