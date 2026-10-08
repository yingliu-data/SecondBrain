"""The routes under AGENT_ENGINE=harness / tenant engine: chat streams the
Worker's sink; tool-result goes to DeviceCalls (409 for unknown ids);
legacy stays untouched."""
import hashlib
import hmac
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import middleware
from app.session.dir_store import DirStore
from app.tenants import Tenant, TenantRegistry
from app.host.sides import DEVICE_CALLS
from app.host.worker import Workers
from host_fakes import FakeRegistry, FakeSandbox, users_root  # noqa: F401


def _headers(key, body: dict):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = hmac.new(key.encode(), f"{ts}{raw}".encode(), hashlib.sha256).hexdigest()
    return raw, {"Authorization": f"Bearer {key}", "X-Timestamp": ts, "X-Signature": sig,
                 "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path, users_root, monkeypatch):
    from app.routes import chat, tool_result
    doc = {"tenants": [
        {"name": "harn", "api_key": "harn-key", "engine": "harness"},
        {"name": "leg", "api_key": "leg-key"},
    ]}
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(doc))
    middleware.set_tenant_registry(TenantRegistry(path, "test-default-key"))
    monkeypatch.setattr(chat, "sessions", DirStore(tmp_path / "sessions"))
    script = [{"call": {"id": "call_x", "name": "get_calendar_events", "args": {}}},
              {"text": "one event"}]
    workers = Workers(sandbox_factory=lambda: FakeSandbox(script), hello_timeout_s=5,
                      wall_clock_s=15)
    monkeypatch.setattr(chat, "workers", workers)
    chat.set_dependencies(FakeRegistry(), None)
    monkeypatch.setattr(chat, "HARNESS_KEEPALIVE_S", 0.2)
    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(tool_result.router)
    with TestClient(app) as c:
        yield c, workers
        # the workers live on the TestClient's loop: close them there
        c.portal.call(workers.shutdown)


def test_tenant_engine_field():
    assert Tenant(name="a", api_key="k").engine is None
    assert Tenant(name="a", api_key="k", engine="harness").engine == "harness"
    with pytest.raises(ValueError):
        Tenant(name="a", api_key="k", engine="other")


def test_example_default_tenant_runs_on_harness_over_the_env_default(tmp_path, monkeypatch):
    """M3-CONTRACTS §7: AGENT_ENGINE stays legacy; the default tenant's entry
    in tenants.json.example says harness, and Tenant.engine wins."""
    import json as _json
    from pathlib import Path

    import app.routes.chat as chat_mod
    from app.tenants import TenantRegistry

    assert chat_mod.AGENT_ENGINE == "legacy"
    doc = _json.loads((Path(__file__).parent.parent / "tenants.json.example").read_text())
    for i, t in enumerate(doc["tenants"]):
        t["api_key"] = "env-key" if t["name"] == "default" else f"key-{i}"
    p = tmp_path / "tenants.json"
    p.write_text(_json.dumps(doc))
    reg = TenantRegistry(p, "env-key")
    default = reg.get_by_api_key("env-key")
    assert default.is_default and default.session_key("abc") == "abc"
    assert sum(1 for t in reg.all_tenants() if t.name == "default") == 1
    assert chat_mod.engine_for(default) == "harness"
    wcc = next(t for t in reg.all_tenants() if t.name == "wcc-event")
    assert chat_mod.engine_for(wcc) == "legacy"                  # no engine: the env default
    monkeypatch.setattr(chat_mod, "AGENT_ENGINE", "harness")
    assert chat_mod.engine_for(Tenant(name="x", api_key="k", engine="legacy")) == "legacy"


def test_harness_chat_streams_and_device_result_resolves(client):
    c, workers = client
    raw, h = _headers("harn-key", {"message": "calendar?", "session_id": "s1",
                                   "input_id": "in_route_1"})
    # TestClient buffers the whole SSE body, so the device answers from a
    # thread through the tool-result route while the chat request is open.
    def answer_device(answers: list):
        def run():
            deadline = time.time() + 5
            while time.time() < deadline and not DEVICE_CALLS.is_open("call_x"):
                time.sleep(0.02)
            raw2, h2 = _headers("harn-key", {"tool_call_id": "call_x", "result": "[]"})
            answers.append(c.post("/api/v1/tool-result", content=raw2, headers=h2))
        t = threading.Thread(target=run)
        t.start()
        return t

    answers: list = []
    t = answer_device(answers)
    r = c.post("/api/v1/chat", content=raw, headers=h)
    t.join(5)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert answers and answers[0].json() == {"status": "ok"}
    body = r.text
    assert "event: tool_call\ndata: " + json.dumps(
        {"id": "call_x", "name": "get_calendar_events", "arguments": {}}) in body
    assert "event: done\ndata: {}" in body and '"text": "one "' in body
    assert "event: seq\ndata: " in body
    # unknown / terminal id -> 409 under the harness engine
    raw3, h3 = _headers("harn-key", {"tool_call_id": "call_x", "result": "again"})
    assert c.post("/api/v1/tool-result", content=raw3, headers=h3).status_code == 409
    # the same input_id again does not run a second guest
    raw, h = _headers("harn-key", {"message": "calendar?", "session_id": "s1",
                                   "input_id": "in_route_1"})
    r = c.post("/api/v1/chat", content=raw, headers=h)
    assert "event: done" in r.text and '"text": "one "' in r.text
    w = workers.get("harn/harn_s1")
    assert w.sandbox.starts == 1
    assert (w.session.root / "events" / "r0001" / "000001.jsonl").exists()
    # a server-minted input_id when the body has none: a second guest run
    raw, h = _headers("harn-key", {"message": "again", "session_id": "s1"})
    t = answer_device(answers)
    r = c.post("/api/v1/chat", content=raw, headers=h)
    t.join(5)
    assert answers[-1].json() == {"status": "ok"}
    assert "event: done" in r.text and '"text": "one "' in r.text
    assert w.sandbox.starts == 2 and w.sync.runs() == [1, 2]
    assert [b["outcome"] for b in w.sync.settled.values()] == ["done", "done"]


def test_legacy_tenant_keeps_legacy_tool_result_semantics(client):
    c, _ = client
    raw, h = _headers("leg-key", {"tool_call_id": "nope", "result": "x"})
    assert c.post("/api/v1/tool-result", content=raw, headers=h).status_code == 404
    assert DEVICE_CALLS.count() == 0
