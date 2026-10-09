"""The agent-api side of the MCP gateway outside the worker: POST /internal/gateway-events,
tenants.json compatibility (`mcp_servers` accepted and ignored)."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway import events
from app.host.state_sync import StateSync
from app.session.dir_store import DirStore
from app.tenants import TenantRegistry

EVENT = {"user": "u", "tenant": "t", "session_id": "s1", "op": "call",
         "tool": "wcc-events__draft_event", "upstream": "wcc-events", "ok": True, "ms": 12}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.routes import chat
    store = DirStore(tmp_path / "sessions")
    monkeypatch.setattr(chat, "sessions", store)
    monkeypatch.setattr(events, "INTERNAL_TOKEN", "tok")
    app = FastAPI()
    app.include_router(events.router)
    return TestClient(app), store


AUTH = {"Authorization": "Bearer tok"}


def test_gateway_event_auth_and_validation(api):
    client, store = api
    assert client.post("/internal/gateway-events", json=EVENT).status_code == 401
    assert client.post("/internal/gateway-events", json=EVENT,
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    bad = dict(EVENT, session_id="../x")
    assert client.post("/internal/gateway-events", json=bad, headers=AUTH).status_code == 400
    assert client.post("/internal/gateway-events", json={"user": "u"},
                       headers=AUTH).status_code == 400


def test_unknown_session_is_dropped_with_404(api):
    client, store = api
    r = client.post("/internal/gateway-events", json=EVENT, headers=AUTH)
    assert r.status_code == 404 and r.json() == {"stored": False}
    assert not (store.root / "u").exists()


def test_without_a_worker_the_event_continues_the_host_log(api):
    client, store = api
    session = store.get_or_create("u", "t", "s1")
    sync = StateSync(session)
    sync.host_event("workflow", {"kind": "task_signal"})       # an earlier worker's
    del sync
    for _ in range(2):
        r = client.post("/internal/gateway-events", json=EVENT, headers=AUTH)
        assert r.status_code == 202 and r.json() == {"stored": True}
    host = StateSync(session).read_host()
    assert [h["hseq"] for h in host] == [1, 2, 3]
    assert host[1]["type"] == "gateway" and host[1]["session_id"] == "s1"
    assert host[1]["body"] == {"op": "call", "tool": "wcc-events__draft_event",
                               "upstream": "wcc-events", "ok": True, "ms": 12}


def test_with_a_live_state_sync_the_event_shares_its_counter(api):
    client, store = api
    session = store.get_or_create("u", "t", "s1")
    sync = StateSync(session)
    sync.host_event("workflow", {"kind": "task_signal"})
    assert client.post("/internal/gateway-events", json=EVENT, headers=AUTH).status_code == 202
    sync.host_event("workflow", {"kind": "completed"})          # one counter, no duplicate hseq
    assert [h["hseq"] for h in sync.read_host()] == [1, 2, 3]
    assert sync.read_host()[1]["type"] == "gateway"


def test_empty_token_means_no_check(api, monkeypatch):
    client, store = api
    monkeypatch.setattr(events, "INTERNAL_TOKEN", "")
    store.get_or_create("u", "t", "s1")
    assert client.post("/internal/gateway-events", json=EVENT).status_code == 202


# ---------------------------------------------------------------- tenants
def test_tenants_file_loads_old_fields_and_ignores_mcp_servers(tmp_path):
    """The top-level map (even one the old schema would reject) and the
    tenant list are accepted and ignored; the gateway owns MCP access."""
    doc = {"mcp_servers": {"wcc-events": {"url": "http://h/mcp", "auth_token": "x",
                                          "timeout_s": 2700, "tool_defs_ttl_s": 60,
                                          "allow_private": True},
                           "bad__name": {}},
           "tenants": [{"name": "a", "api_key": "k", "mcp_servers": ["wcc-events", "ghost"],
                        "promoted": ["wcc-events__draft_event"]},
                       {"name": "b", "api_key": "kb", "mcp_servers": ["wcc-events"],
                        "local_skills": ["web_search"]}]}
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(doc))
    reg = TenantRegistry(path, "default-key")
    assert not hasattr(reg, "mcp_server_configs")
    a = reg.get_by_api_key("k")
    # MCP servers without local_skills: still no local skills (never widened)
    assert a.local_skills == [] and a.allowed_skill_names() == set()
    assert not hasattr(a, "promoted") and not hasattr(a, "mcp_servers")   # dropped, logged
    assert reg.get_by_api_key("kb").allowed_skill_names() == {"web_search"}
