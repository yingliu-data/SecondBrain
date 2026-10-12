"""Host robustness across an agent-api restart, end to end where it matters:
Worker with LocalSandbox (the real guest as a subprocess) and a scripted
upstream LLM.

  - a crash never reruns a model turn: a new process that finds the input
    unsettled in the session's current run resumes that run (death-resume
    path: hello{resume: true} + the log return), it does not open a new one
  - a new input after a restart settles the orphaned task first
  - device results: an adopted call stays open until its result went out
    (a failed delivery is a 503 and may be retried; a duplicate is not sent
    twice)
  - answer_wait_s: the tenant's on either path; unset, 0 on the direct
    path, SessionConfig's default on the workflow path
  - graceful shutdown parks running guests: a restart adopts their open
    device calls
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.auth.middleware import verify
from app.host.sandbox.local import LocalSandbox
from app.host.sides import DeliveryError, DeviceCalls
from app.host.worker import DIRECT, WORKFLOW, Worker, Workers
from app.session.dir_store import DirStore
from host_fakes import FakeRegistry, FakeTenant, FakeUpstreamLLM, session, users_root  # noqa: F401
from app.session.ids import session_ref

REF = session_ref("u", "t", "s1")

GUEST_ROOT = Path(__file__).resolve().parents[2] / "harness"
LOST = "error: operation lost (VM restarted)"


def tool_call(call_id, name, args):
    return {"content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def upstream():
    u = FakeUpstreamLLM([]).start()
    yield u
    u.stop()


WORKER_KW = dict(llm_url=None, hello_timeout_s=20, heartbeat_s=1, wall_clock_s=60,
                 park_grace_s=30, session_idle_s=None, abort_wait_s=20)


def make_worker(session, llm_url, dc, tenant=None):
    return Worker(session, tenant or FakeTenant(), FakeRegistry(), LocalSandbox(GUEST_ROOT),
                  device_calls=dc, **{**WORKER_KW, "llm_url": llm_url})


def make_workers(session, llm_url, dc):
    return Workers(sandbox_factory=lambda: LocalSandbox(GUEST_ROOT),
                   sessions=DirStore(session.root.parents[1]), device_calls=dc,
                   **{**WORKER_KW, "llm_url": llm_url})


async def wait_until(pred, timeout=30.0, what=""):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        if loop.time() - t0 > timeout:
            raise AssertionError(f"timed out waiting for {what or pred}")
        await asyncio.sleep(0.02)


def of_type(w, etype):
    return [e["body"] for e in w.sync.read(0) if e["type"] == etype]


def assert_resumed_once(w, input_id, call_id, marker: Path, upstream):
    """One run, one session_start, the input new once, the tool executed
    once and then lost, one llm request per logged response, one settle."""
    assert w.sync.runs() == [1]
    assert not (w.session.events / "r0002").exists()
    assert len(of_type(w, "session_start")) == 1
    news = [b["input_id"] for b in of_type(w, "input") if b["dedupe"] == "new"]
    assert news == [input_id]
    states = [b["state"] for b in of_type(w, "op_state") if b["call_id"] == call_id]
    assert states.count("running") == 1 and states[-1] == "lost"
    [result] = [b for b in of_type(w, "tool_result") if b["call_id"] == call_id]
    assert result["content"] == LOST
    assert marker.read_text().count("ran") == 1                  # never re-executed
    # the first turn's response was logged: asked once; the second turn once
    assert len(upstream.requests) == 2
    assert upstream.requests[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": call_id, "content": LOST}
    settles = of_type(w, "settle")
    assert [(s["input_id"], s["outcome"]) for s in settles] == [(input_id, "done")]
    resumed = [h["body"] for h in w.sync.read_host() if h["type"] == "workflow"
               and h["body"].get("reason") == "unsettled_input"]
    assert [r["input_id"] for r in resumed] == [input_id]


# ---------------------------------------------------------------- 1. crash
@pytest.mark.asyncio
async def test_process_death_mid_task_then_resend_resumes_the_run(session, upstream, tmp_path):
    """agent-api dies while the guest runs a tool (its turn's llm_response
    is logged): a new process (new Worker, new StateSync on the same
    session directory; the guest is gone) gets the same input again. The
    run is resumed - no events/r0002, no second session_start - the tool
    call is lost (op_state{lost} + error result) and not run again, the
    logged turn is not asked again, the task settles once."""
    marker = tmp_path / "ran.txt"
    upstream.responses = [tool_call("call_b", "bash", {"command": f"echo ran >> {marker}; sleep 60"}),
                          {"content": "finished after the restart"}]
    w1 = make_worker(session, upstream.url, DeviceCalls())
    t1 = asyncio.ensure_future(w1.act({"input_id": "in_c1", "text": "slow"}))
    await wait_until(lambda: marker.exists() and any(
        b["call_id"] == "call_b" and b["state"] == "running" for b in of_type(w1, "op_state")),
        what="the tool runs")
    await w1.close()                    # the process dies: its guest with it
    await asyncio.gather(t1, return_exceptions=True)
    assert w1.sync.open_input == "in_c1" and "in_c1" not in w1.sync.settled

    w2 = make_worker(session, upstream.url, DeviceCalls())
    try:
        assert w2.sync.open_input == "in_c1"
        result = await asyncio.wait_for(w2.act({"input_id": "in_c1", "text": "slow"}), 60)
        assert result["outcome"] == "done" and result["answer"] == "finished after the restart"
        assert w2.last_hello["resume"] is True
        assert_resumed_once(w2, "in_c1", "call_b", marker, upstream)
        # and again: the stored settle, no guest work
        again = await w2.act({"input_id": "in_c1", "text": "slow"})
        assert again["answer"] == result["answer"] and len(upstream.requests) == 2
    finally:
        await w2.close()


@pytest.mark.asyncio
async def test_a_new_input_after_a_restart_settles_the_orphan_on_a_parked_guest(
        session, upstream, monkeypatch):
    """Shutdown parked the guest mid-task (a device call open); after the
    restart a DIFFERENT input arrives: the orphaned task is aborted first
    (the guest settles it), then the new input runs as its own task in the
    same run (not staged into the old one)."""
    upstream.responses = [tool_call("call_o", "get_calendar_events", {}),
                          {"content": "second task answer"}]
    dc1 = DeviceCalls()
    workers1 = make_workers(session, upstream.url, dc1)
    tenant = FakeTenant()
    w1 = workers1.get_or_create(tenant, "s1", FakeRegistry())
    t1 = asyncio.ensure_future(w1.act({"input_id": "in_o1", "text": "calendar"}))
    await wait_until(lambda: dc1.is_open("call_o"), what="device call open")
    await workers1.shutdown(park_s=10)
    await asyncio.gather(t1, return_exceptions=True)
    assert (session.root / "vm" / "parked.json").exists()

    workers2 = make_workers(session, upstream.url, DeviceCalls())
    try:
        w2 = workers2.get_or_create(tenant, "s1", FakeRegistry())
        assert w2.vm_state == "parked" and w2.sync.open_input == "in_o1"
        result = await asyncio.wait_for(w2.act({"input_id": "in_o2", "text": "next"}), 60)
        assert result["outcome"] == "done" and result["answer"] == "second task answer"
        assert w2.sync.settled["in_o1"]["outcome"] == "aborted"
        assert [s["input_id"] for s in of_type(w2, "settle")] == ["in_o1", "in_o2"]
        assert w2.sync.runs() == [1]
        assert [h["body"]["input_id"] for h in w2.sync.read_host()
                if h["type"] == "workflow" and h["body"].get("kind") == "orphan_abort"] == ["in_o1"]
    finally:
        await workers2.shutdown(park_s=0)


# ---------------------------------------------------------------- 2. device results
@pytest.mark.asyncio
async def test_adopted_device_call_stays_open_until_delivered():
    """A deliver that raises leaves the call open (DeliveryError); the
    retry delivers it and only then is it terminal."""
    dc = DeviceCalls()
    delivered, fail = [], [True]

    def deliver(result):
        if fail[0]:
            raise RuntimeError("no guest")
        delivered.append(result)
    dc.adopt("call_a", "get_calendar_events", {}, REF, deliver=deliver,
             loop=asyncio.get_running_loop())
    with pytest.raises(DeliveryError):
        dc.resolve("call_a", "r1", owner=("u", "t"))
    assert dc.is_open("call_a") and dc.open_for(REF)[0]["id"] == "call_a"
    fail[0] = False
    assert dc.resolve("call_a", "r2", owner=("u", "t")) is True
    assert delivered == ["r2"] and not dc.is_open("call_a")
    assert dc.resolve("call_a", "r3") is False                  # terminal now: 409


@pytest.mark.asyncio
async def test_device_result_route_503_then_retry_and_worker_dedupe(session, monkeypatch):
    """POST /tool-result for an adopted call whose delivery fails is a 503
    with the call still open; the retry is a 200. On the Worker a second
    delivery of the same call_id is not sent again."""
    from app.routes import chat, tool_result
    dc = DeviceCalls()
    sent = []
    w = Worker(session, FakeTenant(), FakeRegistry(), LocalSandbox(GUEST_ROOT), device_calls=dc,
               **{**WORKER_KW, "llm_url": "http://127.0.0.1:9"})
    statuses = iter(["no_guest", "held", "held"])

    def fake_send(type, body):
        status = next(statuses)
        sent.append((type, body["call_id"], status))
        return status
    monkeypatch.setattr(w, "send_control", fake_send)
    deliver = lambda result: w._deliver_adopted("call_r", "get_calendar_events", result)  # noqa: E731
    w._adopted["call_r"] = deliver
    dc.adopt("call_r", "get_calendar_events", {}, w.ref, deliver=deliver,
             loop=asyncio.get_running_loop())
    monkeypatch.setattr(tool_result, "DEVICE_CALLS", dc)
    monkeypatch.setattr(chat, "workers", Workers(sessions=DirStore(session.root.parents[1])))
    app = FastAPI()
    app.include_router(tool_result.router)
    app.dependency_overrides[verify] = lambda: FakeTenant()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        r = await client.post("/api/v1/tool-result", json={"tool_call_id": "call_r", "result": "x"})
        assert r.status_code == 503 and dc.is_open("call_r")
        r = await client.post("/api/v1/tool-result", json={"tool_call_id": "call_r", "result": "x"})
        assert r.status_code == 200 and not dc.is_open("call_r")
        assert [s[2] for s in sent] == ["no_guest", "held"]
        # a duplicate delivery (e.g. a deliver retried after it went out) is not resent
        w._deliver_adopted("call_r", "get_calendar_events", "x")
        assert len(sent) == 2
        r = await client.post("/api/v1/tool-result", json={"tool_call_id": "call_r", "result": "x"})
        assert r.status_code == 409
    finally:
        await client.aclose()
        await w.close()


# ---------------------------------------------------------------- 4. answer_wait_s
class _Tenant(FakeTenant):
    def __init__(self, answer_wait_s):
        super().__init__(answer_wait_s=answer_wait_s)


@pytest.mark.parametrize("tenant_value, direct, workflow", [
    (None, 0, 86400), (0, 0, 0), (120, 120, 120)])
def test_answer_wait_for_each_driver(session, tenant_value, direct, workflow):
    w = Worker(session, _Tenant(tenant_value), FakeRegistry(), LocalSandbox(GUEST_ROOT),
               device_calls=DeviceCalls())
    assert w.answer_wait_for(DIRECT) == direct
    assert w.answer_wait_for(WORKFLOW) == workflow
    assert w.answer_wait_s == direct                    # before any boot: the direct default


@pytest.mark.asyncio
async def test_direct_path_hello_without_tenant_answer_wait_denies(session, upstream):
    upstream.responses = [{"content": "hi"}]
    w = make_worker(session, upstream.url, DeviceCalls(), tenant=_Tenant(None))
    try:
        await asyncio.wait_for(w.act({"input_id": "in_aw", "text": "x"}), 60)
        assert w.last_hello["config"]["limits"]["answer_wait_s"] == 0
    finally:
        await w.close()


# ---------------------------------------------------------------- 5. shutdown
@pytest.mark.asyncio
async def test_shutdown_parks_a_running_session_and_a_restart_adopts_its_device_call(
        session, upstream, monkeypatch):
    """Workers.shutdown parks the guest mid-task (a device call open)
    instead of destroying it unparked: after the restart the device's
    result is accepted (the call was adopted from the parked log) and the
    task completes in the same run, the call not lost."""
    from app.routes import chat, tool_result
    upstream.responses = [tool_call("call_s", "get_calendar_events", {"day": "today"}),
                          {"content": "Standup at 9"}]
    dc1 = DeviceCalls()
    workers1 = make_workers(session, upstream.url, dc1)
    w1 = workers1.get_or_create(FakeTenant(), "s1", FakeRegistry())
    t1 = asyncio.ensure_future(w1.act({"input_id": "in_sd", "text": "calendar"}))
    await wait_until(lambda: dc1.is_open("call_s"), what="device call open")
    await workers1.shutdown(park_s=10)
    await asyncio.gather(t1, return_exceptions=True)
    parked = [h["body"] for h in w1.sync.read_host()
              if h["type"] == "workflow" and h["body"].get("kind") == "parked"]
    assert parked and parked[-1]["reason"] == "shutdown"

    dc2 = DeviceCalls()
    workers2 = make_workers(session, upstream.url, dc2)
    monkeypatch.setattr(chat, "workers", workers2)
    monkeypatch.setattr(chat, "_registry", FakeRegistry())
    monkeypatch.setattr(tool_result, "DEVICE_CALLS", dc2)
    app = FastAPI()
    app.include_router(tool_result.router)
    app.dependency_overrides[verify] = lambda: FakeTenant()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        r = await client.post("/api/v1/tool-result",
                              json={"tool_call_id": "call_s", "result": '[{"title": "Standup"}]'})
        assert r.status_code == 200
        w2 = workers2.get("u/t/s1")
        await wait_until(lambda: "in_sd" in w2.sync.settled, 60, "settle after the restart")
        assert w2.sync.settled["in_sd"]["answer"] == "Standup at 9"
        assert w2.sync.runs() == [1]
        assert not any(b["state"] == "lost" for b in of_type(w2, "op_state"))
        assert len(upstream.requests) == 2
    finally:
        await client.aclose()
        await workers2.shutdown(park_s=0)
