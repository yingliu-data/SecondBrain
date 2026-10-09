"""Sides: host skill round trip, device relay through DeviceCalls, mcp is not a side."""
import asyncio
import json

import pytest

from app.host.sides import DeviceCalls, Sides
from host_fakes import FakeRegistry, FakeTenant, session, users_root  # noqa: F401


def call_event(call_id, name, side, args=None):
    return {"session_id": "s", "seq": 7, "ts": "t", "schema": 1, "type": "tool_call",
            "body": {"turn_id": "t", "call_id": call_id, "name": name, "side": side,
                     "args": args or {}, "state": "RUNNING"}}


def make_sides(registry=None, **kw):
    sent, host_events = [], []
    dc = DeviceCalls()
    sides = Sides(registry or FakeRegistry(), FakeTenant(), None, None,
                  send_control=lambda t, b: sent.append((t, b)),
                  host_event=lambda t, b: host_events.append((t, b)),
                  device_calls=dc, session_key="u/t_s1", **kw)
    return sides, sent, host_events, dc


@pytest.mark.asyncio
async def test_host_skill_round_trip():
    reg = FakeRegistry({"web_search": "three results"})
    sides, sent, host_events, _ = make_sides(reg)
    task = sides.handle_tool_call(call_event("c1", "web_search", "host", {"q": "x"}))
    result = await task
    assert reg.calls == [("web_search", {"q": "x"})]
    assert sent == [("side_result", {"call_id": "c1", "side": "host",
                                     "content": "three results", "ok": True})]   # no memory key
    assert result["ok"] and host_events[0][0] == "side_call"
    assert sides.handle_tool_call(call_event("c0", "bash", "guest")) is None


@pytest.mark.asyncio
async def test_host_skill_errors_and_avatar_compaction():
    reg = FakeRegistry({"web_search": RuntimeError("boom"),
                        "set_pose": json.dumps({"frames": [1, 2, 3], "loop": True})})
    noted = {}
    sides, sent, _, _ = make_sides(reg, note_host_result=lambda c, r: noted.__setitem__(c, r))
    await sides.handle_tool_call(call_event("c1", "web_search", "host"))
    assert sent[-1][1]["ok"] is False and sent[-1][1]["content"].startswith("Error")
    await sides.handle_tool_call(call_event("c2", "set_pose", "host"))
    assert json.loads(noted["c2"])["frames"] == [1, 2, 3]
    assert json.loads(sent[-1][1]["content"])["frames"] == [1, 2, 3]   # not compacted
    reg.results["plan_movement"] = json.dumps({"frames": [1, 2, 3], "loop": True})
    await sides.handle_tool_call(call_event("c3", "plan_movement", "host"))
    assert json.loads(sent[-1][1]["content"]) == {"status": "ok", "frames_generated": 3,
                                                  "loop": True}
    assert json.loads(noted["c3"])["frames"] == [1, 2, 3]            # the stream gets the frames


@pytest.mark.asyncio
async def test_host_skill_timeout():
    class Slow(FakeRegistry):
        async def execute_server_tool(self, name, arguments, allowed=None):
            await asyncio.sleep(5)

    sides, sent, _, _ = make_sides(Slow(), host_timeout_s=0.05)
    await sides.handle_tool_call(call_event("c1", "web_search", "host"))
    assert "timed out" in sent[-1][1]["content"] and sent[-1][1]["ok"] is False


@pytest.mark.asyncio
async def test_device_call_resolves_through_registry_and_409_semantics():
    sides, sent, _, dc = make_sides(device_timeout_s=2)
    task = sides.handle_tool_call(call_event("call_9", "get_calendar_events", "device",
                                             {"day": "today"}))
    await asyncio.sleep(0.01)
    assert dc.is_open("call_9")
    assert dc.open_for("u/t_s1") == [{"id": "call_9", "name": "get_calendar_events",
                                       "arguments": {"day": "today"}}]
    assert dc.open_for("other") == []
    assert dc.resolve("nope", "x") is False                     # unknown -> 409 at the route
    assert dc.resolve("call_9", "[{\"title\": \"Standup\"}]") is True
    result = await task
    assert result == {"call_id": "call_9", "side": "device",
                      "content": "[{\"title\": \"Standup\"}]", "ok": True}
    assert dc.resolve("call_9", "again") is False               # terminal -> 409
    assert dc.count() == 0


@pytest.mark.asyncio
async def test_device_timeout_and_mcp_is_not_a_side_call():
    sides, sent, _, dc = make_sides(device_timeout_s=0.05)
    await sides.handle_tool_call(call_event("c1", "get_calendar_events", "device"))
    assert sent[-1][1]["ok"] is False and "timed out" in sent[-1][1]["content"]
    assert not dc.is_open("c1")
    n = len(sent)
    # the guest calls the gateway itself; never a side_result
    assert sides.handle_tool_call(call_event("c2", "wcc__draft", "mcp")) is None
    assert len(sent) == n and not sides.tasks


# ---------------------------------------------------------------- memory
class RememberRegistry(FakeRegistry):
    """The real remember skill behind the registry interface."""

    async def execute_server_tool(self, name, arguments, allowed=None):
        from app.skills.remember.skill import RememberSkill
        self.calls.append((name, arguments))
        return await RememberSkill().execute(name, arguments)


@pytest.mark.asyncio
async def test_remember_result_carries_sha256_and_logs_a_memory_event(session):
    sent, host_events = [], []
    sides = Sides(RememberRegistry(), FakeTenant(), session, None,
                  send_control=lambda t, b: sent.append((t, b)),
                  host_event=lambda t, b: host_events.append((t, b)),
                  device_calls=DeviceCalls(), session_key="u/t_s1")
    args = {"slug": "tz_pref", "name": "Timezone", "description": "prefers London time",
            "type": "user", "body": "Europe/London", "scope": "session"}
    await sides.handle_tool_call(call_event("c_rem", "remember", "host", args))
    result = json.loads(sent[-1][1]["content"])
    path = session.memory / "tz_pref.md"
    digest = result["sha256"]
    assert path.exists() and result["status"] == "ok" and len(digest) == 64
    memory = {"scope": "session", "op": "write", "slug": "tz_pref", "sha256": digest}
    assert sent[-1][1]["memory"] == memory                     # the guest emits it verbatim
    assert ("memory", {**memory, "call_id": "c_rem"}) in host_events
    assert host_events[-1][0] == "side_call"                   # memory first, then the call
    # forget: sha256 is "" and the op is remove
    await sides.handle_tool_call(call_event("c_fgt", "forget", "host",
                                            {"slug": "tz_pref", "scope": "session"}))
    assert json.loads(sent[-1][1]["content"]) == {"status": "ok", "scope": "session",
                                                  "slug": "tz_pref", "sha256": ""}
    memory = {"scope": "session", "op": "remove", "slug": "tz_pref", "sha256": ""}
    assert sent[-1][1]["memory"] == memory
    assert ("memory", {**memory, "call_id": "c_fgt"}) in host_events
    assert not path.exists()
    # forgetting what is not there changes nothing on disk: no memory event
    n = len(host_events)
    await sides.handle_tool_call(call_event("c_fgt2", "forget", "host",
                                            {"slug": "tz_pref", "scope": "session"}))
    assert json.loads(sent[-1][1]["content"])["status"] == "not_found"
    assert "memory" not in sent[-1][1]
    assert [h for h in host_events[n:] if h[0] == "memory"] == []


@pytest.mark.asyncio
async def test_remember_sha256_is_the_stripped_body_hash(session):
    """One memory hash definition everywhere: sha256_hex(body.strip())."""
    from protocol.canonical import sha256_hex

    sent = []
    sides = Sides(RememberRegistry(), FakeTenant(), session, None,
                  send_control=lambda t, b: sent.append((t, b)),
                  device_calls=DeviceCalls(), session_key="u/t_s1")
    args = {"slug": "tz_pref", "name": "Timezone", "description": "prefers London time",
            "type": "user", "body": "  Europe/London\n", "scope": "session"}
    await sides.handle_tool_call(call_event("c_rem", "remember", "host", args))
    assert sent[-1][1]["memory"]["sha256"] == sha256_hex("Europe/London")
