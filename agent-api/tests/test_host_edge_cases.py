"""PLAN.md section 14 edge cases owned by agent-api, end to end: Worker with
LocalSandbox (the real guest as a subprocess) and a scripted upstream LLM.

  - a device result after an agent-api restart is accepted and the task
    completes (the open call is rebuilt from the parked session's log)
  - vLLM down or answering 5xx: the :5010 proxy passes every failure through
    (no retry or auto-start of its own), the guest retries and records one
    `error`, the task settles failed
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.auth.middleware import verify
from app.host.sandbox.local import LocalSandbox
from app.host.sides import DeviceCalls
from app.host.stream import DONE
from app.host.worker import Worker, Workers
from app.session.dir_store import DirStore
from host_fakes import FakeRegistry, FakeTenant, FakeUpstreamLLM, session, users_root  # noqa: F401

GUEST_ROOT = Path(__file__).resolve().parents[2] / "harness"
DEVICE_RESULT = '[{"title": "Standup"}]'


def tool_call(call_id, name, args):
    return {"content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def upstream():
    u = FakeUpstreamLLM([]).start()
    yield u
    u.stop()


def make_worker(session, llm_url, dc):
    return Worker(session, FakeTenant(), FakeRegistry(), LocalSandbox(GUEST_ROOT),
                  device_calls=dc, llm_url=llm_url, hello_timeout_s=20, heartbeat_s=1,
                  wall_clock_s=60, park_grace_s=30, session_idle_s=None)


async def wait_until(pred, timeout=30.0, what=""):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        if loop.time() - t0 > timeout:
            raise AssertionError(f"timed out waiting for {what or pred}")
        await asyncio.sleep(0.02)


async def drain(q):
    out = []
    while True:
        item = await asyncio.wait_for(q.get(), 60)
        if item is None:
            return out
        out.append(item)


class _Gone:
    """Stands in for a crashed agent-api's sandbox handle: closing the old
    Worker must not touch the guest (the frozen process outlives it)."""
    last_vm_event = None

    def stop(self):
        pass

    def is_alive(self):
        return False


def restarted_process(session, llm_url, dc: DeviceCalls, monkeypatch):
    """A new agent-api process: Workers over the session store (no Worker
    for the session yet) and POST /api/v1/tool-result resolving in `dc`, as
    tenant u/t (auth stubbed). (workers, http client)"""
    from app.routes import chat, tool_result
    workers = Workers(sandbox_factory=lambda: LocalSandbox(GUEST_ROOT),
                      sessions=DirStore(session.root.parents[1]), device_calls=dc,
                      llm_url=llm_url, hello_timeout_s=20, heartbeat_s=1, wall_clock_s=60,
                      park_grace_s=30, session_idle_s=None)
    monkeypatch.setattr(chat, "workers", workers)
    monkeypatch.setattr(chat, "_registry", FakeRegistry())
    monkeypatch.setattr(tool_result, "DEVICE_CALLS", dc)
    app = FastAPI()
    app.include_router(tool_result.router)
    app.dependency_overrides[verify] = lambda: FakeTenant()
    return workers, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://test")


# ---------------------------------------------------------------- restart
@pytest.mark.asyncio
@pytest.mark.parametrize("guest", ["kept", "killed"])
async def test_device_result_after_an_agent_api_restart_is_accepted_and_the_task_completes(
        session, upstream, monkeypatch, guest):
    """The task calls a device tool; the guest parks while the call is open
    (side calls do not hold a park); agent-api restarts (new Workers and a
    new DeviceCalls over the same session store, no Worker loaded). POST
    /tool-result for an id it does not know loads the tenant's parked
    sessions; their Workers rebuild the open call from the log, so the result
    is accepted, the guest wakes for the side_result and the task completes;
    an unknown call_id is still 409.

    kept    a crash: the frozen guest outlives the old process and the new
            Worker restores the session snapshot (LocalSandbox adopts the pid)
    killed  a clean shutdown stopped the guest: the session snapshot is gone,
            base restore + hello{resume: true} + a log return ending with
            `parked`, so the guest's device call waits again"""
    upstream.responses = [tool_call("call_d", "get_calendar_events", {"day": "today"}),
                          {"content": "Standup at 9"}]
    dc1 = DeviceCalls()
    w1 = make_worker(session, upstream.url, dc1)
    t1 = asyncio.ensure_future(w1.act({"input_id": "in_rs", "text": "calendar"}))
    await wait_until(lambda: dc1.is_open("call_d"), what="device call open")
    assert await w1.park("requested") is True and w1.vm_state == "parked"
    assert w1.sync.state.open_calls["call_d"]["side"] == "device"
    if guest == "kept":
        w1.sandbox = _Gone()
    await w1.close()
    await asyncio.gather(t1, return_exceptions=True)          # the old process's driver
    assert len(upstream.requests) == 1

    dc2 = DeviceCalls()
    workers, client = restarted_process(session, upstream.url, dc2, monkeypatch)
    try:
        assert workers.get("u/t/s1") is None
        r = await client.post("/api/v1/tool-result", json={"tool_call_id": "call_x", "result": "?"})
        assert r.status_code == 409                                  # unknown id: still 409
        w2 = workers.get("u/t/s1")                                   # ... the parked session loaded
        assert w2 is not None and w2.vm_state == "parked"
        # rebuilt from the log: open, and re-emitted to a stream that attaches
        assert dc2.open_for(w2.ref) == [
            {"id": "call_d", "name": "get_calendar_events", "arguments": {"day": "today"}}]
        sink = w2.attach("in_rs")
        assert sink.get_nowait().startswith("event: tool_call")
        r = await client.post("/api/v1/tool-result",
                              json={"tool_call_id": "call_d", "result": DEVICE_RESULT})
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        await wait_until(lambda: "in_rs" in w2.sync.settled, 60, "settle after the restart")
        # the client re-attaches with the same input_id: the stored settle, no second turn
        result = await w2.act({"input_id": "in_rs", "text": "calendar"})
        assert result["outcome"] == "done" and result["answer"] == "Standup at 9"
        sse = await drain(sink)
        assert sse[-1] == DONE
        assert len(upstream.requests) == 2
        assert upstream.requests[1]["messages"][-1] == {
            "role": "tool", "tool_call_id": "call_d", "content": DEVICE_RESULT}
        events = w2.sync.read(0)
        tr = [e["body"] for e in events if e["type"] == "tool_result"]
        assert [(b["call_id"], b["content"]) for b in tr] == [("call_d", DEVICE_RESULT)]
        assert not any(e["type"] == "op_state" and e["body"]["state"] == "lost" for e in events)
        assert w2.sync.runs() == [1]                                 # one run, continued
        restores = [h["body"].get("source") for h in w2.sync.read_host()
                    if h["type"] == "vm" and h["body"]["op"] == "restore"
                    and h["body"].get("ok", True)]
        assert restores[-1] == ("session" if guest == "kept" else "base")
        adopted = [h["body"] for h in w2.sync.read_host() if h["type"] == "side_call"
                   and h["body"].get("call_id") == "call_d"]
        assert adopted[-2]["state"] == "adopted"
        assert adopted[-1]["adopted"] is True and adopted[-1]["ok"] is True
        # terminal now: the same call_id again is a 409
        r = await client.post("/api/v1/tool-result",
                              json={"tool_call_id": "call_d", "result": "again"})
        assert r.status_code == 409 and dc2.count() == 0
    finally:
        await client.aclose()
        await workers.shutdown()


@pytest.mark.asyncio
async def test_a_restart_after_a_guest_death_does_not_adopt_device_calls(session, upstream):
    """Not parked (the guest was running when agent-api went away): the
    resumed guest would mark the call lost, so nothing is adopted and a
    device result is a 409."""
    upstream.responses = [tool_call("call_d", "get_calendar_events", {}), {"content": "x"}]
    dc1 = DeviceCalls()
    w1 = make_worker(session, upstream.url, dc1)
    t1 = asyncio.ensure_future(w1.act({"input_id": "in_rd", "text": "calendar"}))
    await wait_until(lambda: dc1.is_open("call_d"), what="device call open")
    await w1.close()
    await asyncio.gather(t1, return_exceptions=True)
    dc2 = DeviceCalls()
    w2 = make_worker(session, upstream.url, dc2)
    try:
        assert w2.vm_state == "none" and "call_d" in w2.sync.state.open_calls
        assert dc2.count() == 0 and dc2.resolve("call_d", "late") is False
    finally:
        await w2.close()


# ---------------------------------------------------------------- vLLM down
@pytest.mark.asyncio
@pytest.mark.parametrize("down", ["unreachable", "503"])
async def test_vllm_down_retries_in_the_guest_then_one_error(session, upstream, down):
    """agent-api has no LLM auto-start and the proxy does not retry: every
    attempt the guest makes is answered (502 upstream_unreachable when
    nothing listens, else the upstream's 503 verbatim) and recorded as a
    `usage` host event; the guest's provider retries (3 attempts), then the
    task records exactly one `error` and settles failed; the SSE stream ends
    with one fatal error + done."""
    if down == "unreachable":
        llm_url = "http://127.0.0.1:9"          # discard port: connection refused
    else:
        upstream.status = 503
        llm_url = upstream.url
    w = make_worker(session, llm_url, DeviceCalls())
    sink = w.attach("in_down")
    try:
        result = await asyncio.wait_for(w.act({"input_id": "in_down", "text": "hi"}), 90)
        sse = await drain(sink)
        assert result["outcome"] == "failed"
        usage = [h["body"] for h in w.sync.read_host() if h["type"] == "usage"]
        status = 502 if down == "unreachable" else 503
        assert [u["status"] for u in usage] == [status] * 3
        if down == "unreachable":
            assert all("error" in u for u in usage)
        else:
            assert len(upstream.requests) == 3
        events = w.sync.read(0)
        errors = [e["body"] for e in events if e["type"] == "error"]
        assert len(errors) == 1 and errors[0]["class"] == "transient"
        assert errors[0]["retry"] is False
        settle = [e["body"] for e in events if e["type"] == "settle"]
        assert [s["outcome"] for s in settle] == ["failed"]
        assert settle[0]["reason"] == "provider_error"
        assert len([s for s in sse if s.startswith("event: error")]) == 1
        assert sse[-1] == DONE
    finally:
        await w.close()
