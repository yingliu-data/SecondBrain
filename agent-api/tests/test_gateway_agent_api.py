"""M4 agent-api side outside the worker: POST /internal/gateway-events,
GatewayUpstreamSkill (legacy engine) against a fake gateway, tenants.json
compatibility."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway import events
from app.gateway.upstream_skill import GatewayUpstreamSkill
from app.host.state_sync import StateSync
from app.session.dir_store import DirStore
from app.skills.base import set_current_context
from app.tenants import TenantRegistry
from gateway_fakes import FakeGateway

EVENT = {"user": "u", "tenant": "t", "session_id": "s1", "op": "call",
         "tool": "wcc-events__draft_event", "upstream": "wcc-events", "ok": True, "ms": 12}


class LiveWorker:
    closed = False

    def __init__(self, sync):
        self.sync = sync


class Workers:
    def __init__(self):
        self.by_key = {}

    def get(self, key):
        return self.by_key.get(key)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.routes import chat
    store = DirStore(tmp_path / "sessions")
    workers = Workers()
    monkeypatch.setattr(chat, "sessions", store)
    monkeypatch.setattr(chat, "workers", workers)
    monkeypatch.setattr(events, "INTERNAL_TOKEN", "tok")
    app = FastAPI()
    app.include_router(events.router)
    return TestClient(app), store, workers


AUTH = {"Authorization": "Bearer tok"}


def test_gateway_event_auth_and_validation(api):
    client, store, _ = api
    assert client.post("/internal/gateway-events", json=EVENT).status_code == 401
    assert client.post("/internal/gateway-events", json=EVENT,
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    bad = dict(EVENT, session_id="../x")
    assert client.post("/internal/gateway-events", json=bad, headers=AUTH).status_code == 400
    assert client.post("/internal/gateway-events", json={"user": "u"},
                       headers=AUTH).status_code == 400


def test_unknown_session_is_dropped_with_404(api):
    client, store, _ = api
    r = client.post("/internal/gateway-events", json=EVENT, headers=AUTH)
    assert r.status_code == 404 and r.json() == {"stored": False}
    assert not (store.root / "u").exists()


def test_without_a_worker_the_event_continues_the_host_log(api):
    client, store, _ = api
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


def test_with_a_live_worker_the_event_goes_through_its_state_sync(api):
    client, store, workers = api
    session = store.get_or_create("u", "t", "s1")
    sync = StateSync(session)
    sync.host_event("workflow", {"kind": "task_signal"})
    workers.by_key["u/t_s1"] = LiveWorker(sync)
    assert client.post("/internal/gateway-events", json=EVENT, headers=AUTH).status_code == 202
    sync.host_event("workflow", {"kind": "completed"})          # one counter, no duplicate hseq
    assert [h["hseq"] for h in sync.read_host()] == [1, 2, 3]
    assert sync.read_host()[1]["type"] == "gateway"


def test_empty_token_means_no_check(api, monkeypatch):
    client, store, _ = api
    monkeypatch.setattr(events, "INTERNAL_TOKEN", "")
    store.get_or_create("u", "t", "s1")
    assert client.post("/internal/gateway-events", json=EVENT).status_code == 202


# ---------------------------------------------------------------- legacy skill
def fn(name):
    return {"type": "function", "function": {"name": name, "description": "[via wcc-events MCP] d",
                                             "parameters": {"type": "object"}}}


@pytest.fixture
def gateway():
    gw = FakeGateway().start()
    yield gw
    gw.stop()


@pytest.mark.asyncio
async def test_upstream_skill_definitions_from_the_gateway(gateway):
    gateway.upstream_tools["wcc-events"] = [fn("wcc-events__draft_event"), fn("other__x")]
    skill = GatewayUpstreamSkill("wcc-events", base_url=gateway.url, token="tok")
    assert skill.name == "mcp_wcc-events" and skill.get_tool_definitions() == []
    assert await skill.refresh() is True
    assert [d["function"]["name"] for d in skill.get_tool_definitions()] == ["wcc-events__draft_event"]
    req = gateway.requests[0]
    assert req["path"] == "/internal/upstreams/wcc-events/tools"
    assert req["headers"]["authorization"] == "Bearer tok"
    assert not any(k.startswith("x-sb-") for k in req["headers"])   # session-less


@pytest.mark.asyncio
async def test_upstream_skill_execute_posts_internal_call_with_identity(gateway):
    skill = GatewayUpstreamSkill("wcc-events", base_url=gateway.url, token="tok")
    set_current_context(None, "u", "t", "sess_legacy")
    assert await skill.execute("wcc-events__draft_event", {"a": 1}) == "called"
    req = gateway.requests[-1]
    assert req["path"] == "/internal/call"
    assert json.loads(req["body"]) == {"name": "wcc-events__draft_event", "arguments": {"a": 1}}
    h = req["headers"]
    assert (h["x-sb-user"], h["x-sb-tenant"], h["x-sb-session"]) == ("u", "t", "sess_legacy")
    assert h["authorization"] == "Bearer tok"
    gateway.call_result = {"content": "denied by policy", "is_error": True}
    assert await skill.execute("wcc-events__draft_event", {}) == "Error from 'draft_event': denied by policy"
    assert (await skill.execute("other__x", {})).startswith("Error: tool 'other__x' does not belong")
    gateway.status = 500
    assert (await skill.execute("wcc-events__draft_event", {})).startswith("Error: MCP gateway")


@pytest.mark.asyncio
async def test_upstream_skill_down_gateway_and_missing_identity():
    skill = GatewayUpstreamSkill("wcc-events", base_url="http://127.0.0.1:9")
    assert await skill.refresh() is False and skill.get_tool_definitions() == []
    set_current_context(None, "", "", "")
    assert "needs a user, tenant and session" in await skill.execute("wcc-events__x", {})
    set_current_context(None, "u", "t", "s")
    assert (await skill.execute("wcc-events__x", {})).startswith("Error: MCP gateway unreachable")


# ---------------------------------------------------------------- tenants
def test_tenants_file_loads_old_fields_and_name_only_servers(tmp_path):
    doc = {"mcp_servers": {"wcc-events": {"url": "http://h/mcp", "auth_token": "x",
                                          "timeout_s": 2700, "tool_defs_ttl_s": 60,
                                          "allow_private": True},
                           "wcc-analytic": {}},
           "tenants": [{"name": "a", "api_key": "k", "mcp_servers": ["wcc-events", "wcc-analytic"],
                        "promoted": ["wcc-events__draft_event"]}]}
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(doc))
    reg = TenantRegistry(path, "default-key")
    cfgs = reg.mcp_server_configs()
    assert sorted(cfgs) == ["wcc-analytic", "wcc-events"]
    assert cfgs["wcc-events"].tool_defs_ttl_s == 60 and cfgs["wcc-analytic"].url == ""
    a = reg.get_by_api_key("k")
    assert a.allowed_skill_names() == {"mcp_wcc-events", "mcp_wcc-analytic"}
    assert a.promoted == ["wcc-events__draft_event"]
