"""The guest avatar route on the harness engine: LocalSandbox runs the real
guest, a scripted upstream LLM sits behind the :5010 proxy, the real
avatar_control skill answers on the host side. The page's wire stays
`token` / `avatar_command` / `done`."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from protocol.allowlist import GUEST_BUILTINS

from app.auth.guest import GUEST_SYSTEM_PROMPT, GuestSessionManager
from app.host.sandbox.local import LocalSandbox
from app.host.sides import DeviceCalls
from app.host.worker import Workers
from app.session.dir_store import DirStore
from app.session.ids import session_key
from app.skills.avatar_control import AVATAR_TOOLS
from app.skills.avatar_control.poses import POSES
from app.skills.avatar_control.skill import AvatarControlSkill
from app.skills.registry import SkillRegistry
from app.tenants import TenantRegistry
from host_fakes import FakeUpstreamLLM, users_root, worker_kwargs  # noqa: F401

GUEST_ROOT = Path(__file__).resolve().parents[2] / "harness"
ORIGIN = "https://robot.yingliu.site"


def tool_call(call_id, name, args):
    return {"content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


def events_of(body: str) -> list[tuple[str, object]]:
    """(event, data) per SSE event; comments (keepalive) as (":", None)."""
    out = []
    for block in body.split("\n\n"):
        if not block:
            continue
        if block.startswith(":"):
            out.append((":", None))
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def upstream():
    u = FakeUpstreamLLM([]).start()
    yield u
    u.stop()


@pytest.fixture
def page(tmp_path, users_root, upstream, monkeypatch):  # noqa: F811
    from app.routes import chat, guest
    store = DirStore(tmp_path / "sessions")
    workers = Workers(sandbox_factory=lambda: LocalSandbox(GUEST_ROOT), sessions=store,
                      device_calls=DeviceCalls(),
                      **worker_kwargs(llm_url=upstream.url, hello_timeout_s=20,
                                      heartbeat_s=1, wall_clock_s=60))
    monkeypatch.setattr(chat, "sessions", store)
    monkeypatch.setattr(chat, "workers", workers)
    monkeypatch.setattr(chat, "HARNESS_KEEPALIVE_S", 0.5)
    registry = SkillRegistry.__new__(SkillRegistry)        # no disk discovery
    registry._skills, registry._enabled = {}, {}
    registry.register(AvatarControlSkill())
    chat.set_registry(registry)
    manager = GuestSessionManager()
    monkeypatch.setattr(guest, "guest_manager", manager)
    tenant = TenantRegistry(tmp_path / "none.json", "env-key").guest()
    guest.set_tenant(tenant)
    manager.on_expire = guest._close_session
    app = FastAPI()
    app.include_router(guest.router)
    with TestClient(app) as c:
        yield c, workers, manager, tenant
        c.portal.call(workers.shutdown)


def post(c, body, origin=ORIGIN):
    return c.post("/api/v1/guest/chat", json=body, headers={"Origin": origin})


def test_origin_and_body_checks(page):
    c, workers, _, _ = page
    assert post(c, {"message": "wave", "session_id": "g1"}, origin="https://evil.example").status_code == 403
    assert c.post("/api/v1/guest/chat", json={"message": "wave", "session_id": "g1"}).status_code == 403
    assert post(c, {"message": "  ", "session_id": "g1"}).status_code == 400
    assert post(c, {"message": "wave"}).status_code == 400
    assert workers._workers == {}


def test_guest_avatar_turn_on_the_harness_engine(page, upstream):
    c, workers, _, tenant = page
    upstream.responses = [tool_call("call_pose", "set_pose", {"pose_name": "wave_right"}),
                          {"content": "Waving at you now"}]
    r = post(c, {"message": "wave at me", "session_id": "page-1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    evs = events_of(r.text)
    # the page's wire only: no seq / tool_call / question / error events
    assert {e for e, _ in evs} <= {"token", "avatar_command", "done", ":"}
    assert evs[-1] == ("done", {})
    avatar = [d for e, d in evs if e == "avatar_command"]
    assert avatar == [{"name": "set_pose", "result": {"type": "pose", "joints": POSES["wave_right"],
                                                      "duration_ms": 500}}]
    text = "".join(d["text"] for e, d in evs if e == "token")
    assert text.strip() == "Waving at you now"
    # the guest profile reached the guest: its prompt, avatar tools only, no built-ins
    req = upstream.requests[0]
    assert req["messages"][0]["content"].startswith(GUEST_SYSTEM_PROMPT.split("\n")[0])
    assert {t["function"]["name"] for t in req["tools"]} == set(AVATAR_TOOLS)
    w = workers.get(session_key(tenant.user, tenant.name, "page-1"))
    hello = w.last_hello["config"]
    assert set(hello["exclude_builtins"]) == set(GUEST_BUILTINS)
    assert hello["limits"]["max_tools"] == 2
    assert w.session.root.parent.name == "guest"           # sessions/guest/guest_page-1
    events = w.sync.read(0)
    start = next(e for e in events if e["type"] == "session_start")
    assert {t["name"] for t in start["body"]["tools"]} == set(AVATAR_TOOLS)
    # a second message continues the same harness session
    upstream.responses = [{"content": "Still here"}]
    r = post(c, {"message": "hello again", "session_id": "page-1"})
    evs = events_of(r.text)
    assert "".join(d["text"] for e, d in evs if e == "token").strip() == "Still here"
    assert evs[-1] == ("done", {})
    msgs = upstream.requests[-1]["messages"]
    assert any(m.get("role") == "user" and "wave at me" in str(m.get("content")) for m in msgs)


def test_one_tool_round_then_a_final_answer(page, upstream):
    """max_tools 2 (the old loop's single tool round): further calls are
    refused and a final turn without tools answers."""
    c, _, _, _ = page
    upstream.responses = [tool_call("c1", "set_pose", {"pose_name": "bow"}),
                          tool_call("c2", "set_pose", {"pose_name": "nod_down"}),
                          tool_call("c3", "set_pose", {"pose_name": "shrug"}),
                          {"content": "Done moving"}]
    r = post(c, {"message": "bow, nod, shrug", "session_id": "page-2"})
    evs = events_of(r.text)
    assert [d["name"] for e, d in evs if e == "avatar_command"] == ["set_pose", "set_pose"]
    assert evs[-1] == ("done", {})
    assert "tools" not in upstream.requests[-1] or not upstream.requests[-1]["tools"]


def test_page_limits_and_expiry_close_the_worker(page, upstream):
    c, workers, manager, tenant = page
    upstream.responses = [{"content": "hi"}]
    assert post(c, {"message": "hi", "session_id": "page/3"}).status_code == 200
    from app.routes.guest import harness_session_id
    sid = harness_session_id("page/3")
    assert sid.startswith("g_") and sid != "page/3"
    key = session_key(tenant.user, tenant.name, sid)
    assert workers.get(key) is not None
    manager._sessions["page/3"].message_count = 10
    r = post(c, {"message": "again", "session_id": "page/3"})
    assert r.status_code == 429 and "Message limit" in r.json()["error"]
    created = manager._sessions["page/3"].created_at
    c.portal.call(manager.cleanup, created + 3600)
    assert "page/3" not in manager._sessions
    assert workers.get(key) is None
