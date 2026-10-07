"""Sides: host skill round trip, device relay through DeviceCalls, mcp stub."""
import asyncio
import json

import pytest

from app.host.sides import DeviceCalls, Sides
from host_fakes import FakeRegistry, FakeTenant


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
                                     "content": "three results", "ok": True})]
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
async def test_device_timeout_and_mcp_stub():
    sides, sent, _, dc = make_sides(device_timeout_s=0.05)
    await sides.handle_tool_call(call_event("c1", "get_calendar_events", "device"))
    assert sent[-1][1]["ok"] is False and "timed out" in sent[-1][1]["content"]
    assert not dc.is_open("c1")
    await sides.handle_tool_call(call_event("c2", "wcc__draft", "mcp"))
    assert sent[-1][1] == {"call_id": "c2", "side": "mcp", "content": "mcp side not available",
                           "ok": False}
