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


# ── Agent-loop trace emission ─────────────────────────────────

class ToolLLM:
    """First call returns a tool call, second call returns final text."""
    def __init__(self):
        self.calls = 0

    async def chat_completion(self, messages, tools=None, max_tokens=None,
                              chat_template_kwargs=None):
        self.calls += 1
        if self.calls == 1:
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "tc1", "function": {
                    "name": "fake_tool", "arguments": '{"x": 1}'}}],
            }}]}
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": "done"}}]}


class ToolRegistry:
    def get_tools_for_query(self, query, allowed=None):
        return [{"type": "function", "function": {"name": "fake_tool",
                                                  "description": "", "parameters": {}}}]

    def get_server_tool_names(self, allowed=None):
        return {"fake_tool"}

    def get_device_tool_names(self, allowed=None):
        return set()

    async def execute_server_tool(self, name, args, allowed=None):
        return "tool output"


@pytest.mark.asyncio
async def test_loop_emits_trace_events():
    from app.agent.loop import run_agent_loop
    events = []

    def trace(event, detail=None, duration_ms=None):
        events.append((event, detail, duration_ms))

    async for _ in run_agent_loop("go", [], ToolRegistry(), ToolLLM(), trace=trace):
        pass

    names = [e[0] for e in events]
    assert names == ["tool_call", "tool_result", "assistant_response"]
    call = events[0][1]
    assert call["name"] == "fake_tool" and call["arguments"] == {"x": 1}
    result_event = events[1]
    assert result_event[1]["result"] == "tool output"
    assert isinstance(result_event[2], int)  # duration_ms measured


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
