"""End to end: Worker.act() with LocalSandbox (the real guest as a subprocess
on the session's Unix sockets) and a scripted upstream LLM behind the :5010
proxy. Skipped while harness/guest/coordinator.py is a stub."""
import asyncio
import json
from pathlib import Path

import pytest

import guest.coordinator as guest_coordinator
from protocol.fold import replay

from app.host.sandbox.local import LocalSandbox
from app.host.sides import DeviceCalls
from app.host.stream import DONE, tokens
from app.host.worker import Worker
from host_fakes import FakeRegistry, FakeTenant, FakeUpstreamLLM, session, users_root  # noqa: F401

GUEST_ROOT = Path(__file__).resolve().parents[2] / "harness"

def tool_call(call_id, name, args):
    return {"content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def upstream():
    u = FakeUpstreamLLM([]).start()
    yield u
    u.stop()


def make_worker(session, upstream, registry=None, dc=None):
    return Worker(session, FakeTenant(system_prompt="You are a test. {current_time}"),
                  FakeRegistry() if registry is None else registry, LocalSandbox(GUEST_ROOT),
                  device_calls=DeviceCalls() if dc is None else dc,
                  llm_url=upstream.url, hello_timeout_s=20, heartbeat_s=1, wall_clock_s=60)


async def drain(q):
    out = []
    while True:
        item = await asyncio.wait_for(q.get(), 60)
        if item is None:
            return out
        out.append(item)


@pytest.mark.asyncio
async def test_e2e_text_answer(session, upstream):
    upstream.responses = [{"content": "Hello from the guest"}]
    w = make_worker(session, upstream)
    sink = w.attach()
    try:
        result = await w.act({"input_id": "in_e2e_1", "text": "say hello"})
        sse = await drain(sink)
        assert result["outcome"] == "done", result
        assert result["answer"] == "Hello from the guest"
        assert sse[-1] == DONE and tokens("Hello from the guest") == sse[-5:-1]
        # the proxy saw the guest's request and recorded usage
        req = upstream.requests[0]
        assert req["messages"][0]["role"] == "system"
        assert "You are a test." in req["messages"][0]["content"]
        assert "{current_time}" not in req["messages"][0]["content"]
        assert {t["function"]["name"] for t in req["tools"]} >= {"bash", "web_search",
                                                                   "get_calendar_events"}
        usage = [h for h in w.sync.read_host() if h["type"] == "usage"]
        assert usage and usage[0]["body"]["prompt_tokens"] == 101
        # the stored log replays cleanly and matches what the guest recorded
        events = w.sync.read(0)
        state, problems = replay(events)
        assert problems == []
        assert events[0]["type"] == "session_start" and events[-1]["type"] == "settle"
        assert session.read_history()[-1] == {"role": "assistant", "content": "Hello from the guest"}
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_e2e_bash_tool_runs_in_the_workspace(session, upstream):
    upstream.responses = [tool_call("call_bash_1", "bash", {"command": "echo hi > out.txt; cat out.txt"}),
                          {"content": "The command printed hi"}]
    w = make_worker(session, upstream)
    try:
        result = await w.act({"input_id": "in_e2e_2", "text": "run it"})
        assert result["outcome"] == "done", result
        assert (session.workspace / "out.txt").read_text().strip() == "hi"
        events = w.sync.read(0)
        tr = next(e for e in events if e["type"] == "tool_result")
        assert "hi" in tr["body"]["content"]
        # the second request carried the tool message
        assert upstream.requests[1]["messages"][-1]["role"] == "tool"
        assert [t["event"] for t in session.read_trace()].count("tool_call") == 1
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_e2e_host_and_device_tools(session, upstream):
    upstream.responses = [tool_call("call_h", "web_search", {"q": "x"}),
                          tool_call("call_d", "get_calendar_events", {"day": "today"}),
                          {"content": "Standup at 9"}]
    reg = FakeRegistry({"web_search": "search says 42"})
    dc = DeviceCalls()
    w = make_worker(session, upstream, reg, dc)
    sink = w.attach()
    try:
        task = asyncio.ensure_future(w.act({"input_id": "in_e2e_3", "text": "calendar"}))
        while True:
            item = await asyncio.wait_for(sink.get(), 60)
            if item.startswith("event: tool_call"):
                break
        assert json.loads(item.split("data: ", 1)[1]) == {
            "id": "call_d", "name": "get_calendar_events", "arguments": {"day": "today"}}
        while not dc.is_open("call_d"):
            await asyncio.sleep(0.01)
        assert dc.resolve("call_d", "[{\"title\": \"Standup\"}]")
        result = await task
        assert result["outcome"] == "done", result
        assert reg.calls == [("web_search", {"q": "x"})]
        results = {e["body"]["call_id"]: e["body"]["content"]
                   for e in w.sync.read(0) if e["type"] == "tool_result"}
        assert results == {"call_h": "search says 42", "call_d": "[{\"title\": \"Standup\"}]"}
        assert upstream.requests[2]["messages"][-1] == {
            "role": "tool", "tool_call_id": "call_d", "content": "[{\"title\": \"Standup\"}]"}
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_e2e_redelivered_input_runs_once(session, upstream):
    upstream.responses = [{"content": "once"}]
    w = make_worker(session, upstream)
    try:
        first = await w.act({"input_id": "in_e2e_4", "text": "hi"})
        sink = w.attach()
        again = await w.act({"input_id": "in_e2e_4", "text": "hi"})
        sse = await drain(sink)
        assert first == again and first["outcome"] == "done"
        assert len(upstream.requests) == 1               # one model turn
        assert sse == tokens("once") + [DONE]
        assert w.sync.runs() == [1]
    finally:
        await w.close()
