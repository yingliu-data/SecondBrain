import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import middleware
from app.routes import sessions as sessions_route
from app.session.dir_store import DirStore
from app.tenants import TenantRegistry


# ── Trace endpoints: tenant scoping ───────────────────────────

TWO_TENANT_DOC = {
    "tenants": [
        {"name": "wcc-event", "user": "wcc", "api_key": "key-event"},
        {"name": "wcc-analytic", "user": "wcc", "api_key": "key-analytic"},
    ]
}


def signed_get(client, path, key):
    ts = str(int(time.time()))
    sig = hmac.new(key.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return client.get(path, headers={
        "Authorization": f"Bearer {key}", "X-Timestamp": ts, "X-Signature": sig})


@pytest.fixture
def client(tmp_path):
    doc_path = tmp_path / "tenants.json"
    doc_path.write_text(json.dumps(TWO_TENANT_DOC))
    middleware.set_tenant_registry(TenantRegistry(doc_path, "env-key"))

    store = DirStore(tmp_path / "sessions")
    session = store.get_or_create("wcc", "wcc-event", "s1")
    session.append_trace("tool_call", {"name": "wcc-events__draft_event"})
    session.append_trace("tool_result", {"name": "wcc-events__draft_event",
                                         "duration_ms": 42})
    sessions_route.set_sessions(store)

    app = FastAPI()
    app.include_router(sessions_route.router)
    return TestClient(app)


def test_session_traces_visible_to_owner(client):
    r = signed_get(client, "/api/v1/sessions/s1/traces", "key-event")
    assert r.status_code == 200
    events = [t["event"] for t in r.json()["traces"]]
    assert events == ["tool_call", "tool_result"]


def test_traces_recent_answers_last_minutes(client):
    r = signed_get(client, "/api/v1/sessions/traces/recent?since_minutes=2", "key-event")
    assert r.status_code == 200
    traces = r.json()["traces"]
    assert traces and all(t["session_id"] == "s1" for t in traces)


def test_sibling_tenant_cannot_see_traces(client):
    """Same user, different tenant token → that tenant sees nothing."""
    r = signed_get(client, "/api/v1/sessions/s1/traces", "key-analytic")
    assert r.status_code == 404
    r = signed_get(client, "/api/v1/sessions/traces/recent", "key-analytic")
    assert r.status_code == 200 and r.json()["traces"] == []
    r = signed_get(client, "/api/v1/sessions", "key-analytic")
    assert r.json()["sessions"] == []


def test_list_sessions_scoped(client):
    r = signed_get(client, "/api/v1/sessions", "key-event")
    assert [s["session_id"] for s in r.json()["sessions"]] == ["s1"]
