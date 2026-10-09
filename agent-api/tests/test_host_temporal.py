"""M5: POST /chat over the session workflow (harness/docs/M5-CONTRACTS.md).

The REAL flows.SessionWorkflow on the Temporal time-skipping test server
(the same environment flows/flows/tests/conftest.py starts), the real host
activities (app/host/activities.py), LocalSandbox with the real guest from
../harness and a scripted upstream LLM. Every test runs on the one session
event loop: the Temporal worker, the session Workers and the ASGI app share it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from flows.activities import Deliver, RunResult, RunTask, SessionRef
from flows.client import session_handle

from app.auth import middleware
from app.host import temporal
from app.host.activities import HostActivities, ticket_id_for, worker_config
from app.host.sandbox.local import LocalSandbox
from app.host.sides import DeviceCalls
from app.host.worker import Workers
from app.session.dir_store import DirStore
from app.tenants import TenantRegistry
from host_fakes import FakeRegistry, FakeUpstreamLLM, users_root  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

GUEST_ROOT = Path(__file__).resolve().parents[2] / "harness"
ANSWER_WAIT_S = 3600


def tool_call(call_id, name, args):
    return {"content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


async def wait_until(pred, timeout: float = 30.0, what: str = ""):
    t0 = time.monotonic()
    while True:
        value = pred()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out waiting for {what or pred}")
        await asyncio.sleep(0.05)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def tenv():
    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as e:  # noqa: BLE001 - no download / no binary
        pytest.skip(f"no Temporal time-skipping test server: {e!r}")
    yield env
    await env.shutdown()


class Harness:
    """One test's app: tenant `harn` (engine harness), DirStore, Workers on
    LocalSandbox, the Temporal worker on its own task queue."""

    def __init__(self, env, tmp_path, upstream, monkeypatch):
        from app.routes import chat, sessions
        self.env = env
        self.upstream = upstream
        self.session_id = "s-" + uuid.uuid4().hex[:8]
        self.ref = SessionRef(user="harn", tenant="harn", session_id=self.session_id)
        doc = {"tenants": [{"name": "harn", "api_key": "harn-key", "engine": "harness",
                            "answer_wait_s": ANSWER_WAIT_S, "wall_clock_s": 120}]}
        path = tmp_path / "tenants.json"
        path.write_text(json.dumps(doc))
        self.tenants = TenantRegistry(path, "test-default-key")
        middleware.set_tenant_registry(self.tenants)
        # short: the session's ipc/v.sock_NNNN path must fit AF_UNIX
        self.short = Path(tempfile.mkdtemp(prefix="m5", dir="/tmp"))
        self.store = DirStore(self.short)
        self.workers = Workers(sandbox_factory=lambda: LocalSandbox(GUEST_ROOT),
                               sessions=self.store, config_for=worker_config,
                               device_calls=DeviceCalls(), llm_url=upstream.url,
                               hello_timeout_s=20, heartbeat_s=1, wall_clock_s=120,
                               park_grace_s=30)
        monkeypatch.setattr(chat, "sessions", self.store)
        monkeypatch.setattr(chat, "workers", self.workers)
        monkeypatch.setattr(chat, "HARNESS_KEEPALIVE_S", 0.5)
        sessions.set_sessions(self.store)
        self.registry = FakeRegistry()
        chat.set_dependencies(self.registry, None)
        self.acts = HostActivities(self.workers, self.tenants, self.registry,
                                   abort_wait_s=20)
        app = FastAPI()
        app.include_router(chat.router)
        app.include_router(sessions.router)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://test", timeout=120)
        self.queue = "test-" + uuid.uuid4().hex[:8]

    async def start(self, activities=None):
        ok = await temporal.start(activities or self.acts.activities(), None,
                                  connected=self.env.client, queue=self.queue,
                                  max_heartbeat_throttle_interval=timedelta(seconds=0.5),
                                  default_heartbeat_throttle_interval=timedelta(seconds=0.5))
        assert ok and temporal.enabled()

    @property
    def worker(self):
        return self.workers.get(f"harn/harn_{self.session_id}")

    def _headers(self, body: dict | None):
        raw = json.dumps(body) if body is not None else ""
        ts = str(int(time.time()))
        sig = hmac.new(b"harn-key", f"{ts}{raw}".encode(), hashlib.sha256).hexdigest()
        return raw, {"Authorization": "Bearer harn-key", "X-Timestamp": ts,
                     "X-Signature": sig, "Content-Type": "application/json"}

    async def chat(self, **body) -> str:
        raw, h = self._headers({"session_id": self.session_id, **body})
        r = await self.http.post("/api/v1/chat", content=raw, headers=h)
        assert r.status_code == 200
        return r.text

    async def get(self, path: str):
        raw, h = self._headers(None)
        return await self.http.get(f"/api/v1/sessions/{self.session_id}{path}",
                                   headers={k: v for k, v in h.items() if k != "Content-Type"})

    async def post(self, path: str):
        raw, h = self._headers(None)
        return await self.http.post(f"/api/v1/sessions/{self.session_id}{path}",
                                    content=raw, headers=h)

    async def status(self):
        return await temporal.status(self.ref)

    async def wait_state(self, state: str, timeout: float = 30.0):
        async def ok():
            st = await self.status()
            return st if st is not None and st.state == state else None
        return await wait_until(ok, timeout, f"workflow {state}")

    def host_events(self, kind: str) -> list[dict]:
        return [h["body"] for h in self.worker.sync.read_host()
                if h["type"] == "workflow" and h["body"].get("kind") == kind]

    def tickets(self) -> list[dict]:
        out = []
        for p in sorted(self.worker.session.tickets.glob("ticket_*.json")):
            out.append(json.loads(p.read_text()))
        return out

    async def close(self):
        try:
            st = await self.status()
            if st is not None:
                if st.state == "running":
                    await self.wait_state("idle", 30)
                await session_handle(self.env.client, self.ref).terminate("test done")
        except Exception as e:  # noqa: BLE001
            print(f"teardown: {e!r}")
        await temporal.stop()
        await self.workers.shutdown()
        await self.http.aclose()
        shutil.rmtree(self.short, ignore_errors=True)


@pytest.fixture
def upstream():
    u = FakeUpstreamLLM([]).start()
    yield u
    u.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def h(tenv, tmp_path, users_root, upstream, monkeypatch):
    harness = Harness(tenv, tmp_path, upstream, monkeypatch)
    yield harness
    await harness.close()


def sse_events(body: str) -> list[tuple[str, dict]]:
    out = []
    for frame in body.split("\n\n"):
        if frame.startswith("event: "):
            name, data = frame.split("\n", 1)
            out.append((name[len("event: "):], json.loads(data[len("data: "):])))
    return out


def token_text(body: str) -> str:
    return "".join(d["text"] for e, d in sse_events(body) if e == "token")


# --------------------------------------------------------------------------- #


async def test_chat_task_completes_through_the_workflow(h):
    h.upstream.responses = [{"content": "Hello through Temporal"}]
    await h.start()
    body = await h.chat(message="say hello", input_id="in_t1")
    events = sse_events(body)
    assert events[-1] == ("done", {})
    assert not any(e == "error" for e, _ in events)
    assert token_text(body).strip() == "Hello through Temporal"
    w = h.worker
    assert w.last_hello["config"]["limits"]["answer_wait_s"] == ANSWER_WAIT_S
    st = await h.wait_state("idle")
    assert st.tasks_run == 1 and st.seen == 1
    [ticket] = h.tickets()
    assert ticket == {"ticket_id": ticket_id_for(h.ref, "in_t1"), "input_id": "in_t1",
                      "outcome": "done", "answer": "Hello through Temporal",
                      "question_id": None, "manifest": ticket["manifest"],
                      "usage": ticket["usage"]}
    starts = [b["activity"] for b in h.host_events("activity_start")]
    assert starts == ["prepare_vm", "run_task", "deliver"]
    assert [b["outcome"] for b in h.host_events("delivered")] == ["done"]
    # the same input_id again: the stored answer, no second turn
    again = await h.chat(message="say hello", input_id="in_t1")
    assert token_text(again).strip() == "Hello through Temporal" and "event: done" in again
    assert len(h.upstream.requests) == 1
    # GET /sessions/{id}: the workflow status
    r = await h.get("")
    assert r.status_code == 200 and r.json()["workflow"]["state"] == "idle"
    # GET /sessions/{id}/events?after_seq=N
    r = await h.get("/events?after_seq=2")
    events = r.json()
    assert events and all(e["seq"] > 2 for e in events) and events[-1]["type"] == "settle"
    assert [e["seq"] for e in events] == list(range(3, w.sync.last_seq + 1))


async def test_question_parks_then_answer_resumes(h, monkeypatch):
    monkeypatch.setattr("app.host.worker.HARNESS_QUESTION_TIMEOUT_S", 1)
    h.upstream.responses = [tool_call("call_q", "question", {"text": "Which colour?",
                                                            "options": ["red", "blue"]}),
                            {"content": "You picked blue"}]
    await h.start()
    first = asyncio.ensure_future(h.chat(message="ask me", input_id="in_q1"))
    st = await h.wait_state("parked")
    assert st.current_input_id == "in_q1" and st.question_id
    w = h.worker
    assert w.vm_state == "parked" and w.question_park == {"input_id": "in_q1",
                                                          "question_id": st.question_id}
    parked = h.host_events("parked")
    assert parked[-1]["reason"] == "question" and parked[-1]["question_id"] == st.question_id
    # an attaching stream gets the open question again (M5 §3)
    sink = w.attach("in_q1")
    item = sink.get_nowait()
    w.detach(sink)
    name, data = sse_events(item)[0]
    assert name == "question" and data["question_id"] == st.question_id
    assert data["text"] == "Which colour?" and data["options"] == ["red", "blue"]
    # the answer: parked on that question -> `answer` signal; streams the continuation
    second = await h.chat(message="blue", answer_to=st.question_id, input_id="in_a1")
    body = await asyncio.wait_for(first, 60)
    for b in (body, second):
        assert sse_events(b)[-1] == ("done", {})
        assert "You picked blue" in token_text(b)
    assert ("question", data) in [(e, {k: d[k] for k in data}) for e, d in sse_events(body)
                                  if e == "question"]
    await h.wait_state("idle")
    assert w.sync.settled["in_q1"]["outcome"] == "done"
    assert h.upstream.requests[1]["messages"][-1]["content"] == "blue"
    [ticket] = h.tickets()
    assert ticket["outcome"] == "done" and ticket["question_id"] == st.question_id
    runs = [b for b in h.host_events("activity_start") if b["activity"] == "run_task"]
    assert [r["resume"] for r in runs] == [False, True]


async def test_answer_wait_timer_escalates(h, monkeypatch):
    monkeypatch.setattr("app.host.worker.HARNESS_QUESTION_TIMEOUT_S", 1)
    h.upstream.responses = [tool_call("call_q", "question", {"text": "Still there?"}),
                            {"content": "never"}]
    await h.start()
    first = asyncio.ensure_future(h.chat(message="ask", input_id="in_q2"))
    await h.wait_state("parked")
    await h.env.sleep(ANSWER_WAIT_S + 1)          # the workflow's answer timer
    body = await asyncio.wait_for(first, 60)
    events = sse_events(body)
    assert events[-1] == ("done", {})
    assert any(e == "error" and d["outcome"] == "escalated" for e, d in events)
    await h.wait_state("idle")
    w = h.worker
    # the guest wrote the settle itself after wake + abort{escalated}
    assert w.sync.settled["in_q2"]["outcome"] == "escalated"
    [ticket] = h.tickets()
    assert ticket["outcome"] == "escalated" and ticket["input_id"] == "in_q2"
    assert len(h.upstream.requests) == 1


async def test_run_task_retry_reattaches_without_a_second_turn(h):
    h.upstream.responses = [tool_call("call_b", "bash", {"command": "sleep 2; echo slow"}),
                            {"content": "finished once"}]
    acts = h.acts

    @activity.defn(name="run_task")
    async def flaky_run_task(inp: RunTask) -> RunResult:
        if activity.info().attempt == 1:
            # the turn starts, then the activity dies (a worker crash)
            w = acts.worker(inp.session)
            w.start_task({"input_id": inp.input_id, "text": inp.text, "kind": inp.kind})
            activity.heartbeat({"last_seq": w.sync.last_seq})
            await asyncio.sleep(0.5)
            raise ApplicationError("simulated worker failure")
        return await acts.run_task(inp)

    await h.start([acts.prepare_vm, flaky_run_task, acts.park_vm, acts.deliver])
    body = await h.chat(message="slow one", input_id="in_r1")
    assert sse_events(body)[-1] == ("done", {})
    assert "finished once" in token_text(body)
    await h.wait_state("idle")
    w = h.worker
    news = [e for e in w.sync.read(0) if e["type"] == "input" and e["body"]["dedupe"] == "new"]
    assert [e["body"]["input_id"] for e in news] == ["in_r1"]
    assert len([e for e in w.sync.read(0) if e["type"] == "settle"]) == 1
    assert len(h.upstream.requests) == 2              # tool turn + answer, once each
    retry = [b for b in h.host_events("activity_start") if b["activity"] == "run_task"]
    assert retry and retry[-1]["attempt"] == 2 and "floor_seq" in retry[-1]


async def test_abort_signal_aborts_the_running_task(h):
    h.upstream.responses = [tool_call("call_s", "bash", {"command": "sleep 60"}),
                            {"content": "too late"}]
    await h.start()
    first = asyncio.ensure_future(h.chat(message="long", input_id="in_ab"))
    await h.wait_state("running")
    await wait_until(lambda: h.worker is not None and any(
        e["type"] == "tool_call" for e in h.worker.sync.read(0)), 30, "tool call")
    r = await h.post("/abort")
    assert r.status_code == 200 and r.json()["signal"] == "abort"
    body = await asyncio.wait_for(first, 60)
    events = sse_events(body)
    assert events[-1] == ("done", {})
    assert any(e == "error" and d["outcome"] == "aborted" for e, d in events)
    await h.wait_state("idle")
    settle = h.worker.sync.settled["in_ab"]
    assert settle["outcome"] == "aborted" and settle["answer"] == ""
    [ticket] = h.tickets()
    assert ticket["outcome"] == "aborted"
    assert len(h.upstream.requests) == 1


async def test_deliver_is_idempotent(h):
    h.upstream.responses = [{"content": "once"}]
    await h.start()
    await h.chat(message="hi", input_id="in_d1")
    await h.wait_state("idle")
    w = h.worker
    [ticket] = h.tickets()
    sink = w.attach("in_d1")
    env = ActivityEnvironment()
    for _ in range(2):
        await env.run(h.acts.deliver, Deliver(session=h.ref, input_id="in_d1", outcome="done"))
    assert h.tickets() == [ticket]
    assert len(h.host_events("delivered")) == 1
    items = []
    while not sink.empty():
        items.append(sink.get_nowait())
    assert items.count(None) == 2 and all(i is None or "event: done" in i for i in items)


async def test_answer_without_a_parked_question_streams_an_error(h):
    await h.start()
    body = await h.chat(message="x", answer_to="question_nope", input_id="in_n1")
    events = sse_events(body)
    assert ("error", {"class": "fatal",
                      "message": "No question is waiting for this answer."}) in events
    assert events[-1] == ("done", {})


async def test_abort_without_workflow_is_404(h):
    await h.start()
    h.store.get_or_create("harn", "harn", h.session_id)
    r = await h.post("/abort")
    assert r.status_code == 404
    r = await h.get("")
    assert r.status_code == 200 and r.json()["workflow"] is None


async def test_unreachable_temporal_leaves_the_direct_path(monkeypatch):
    monkeypatch.setattr("app.host.temporal.TEMPORAL_CONNECT_TIMEOUT_S", 2)
    assert await temporal.start([], None, address="127.0.0.1:1") is False
    assert not temporal.enabled()
    assert await temporal.start([], None, address="") is False


async def test_tenant_workflow_entry_and_doc(tmp_path):
    from flows.activities import SessionConfig
    from flows.schedules import entries_from_tenants
    doc = {"tenants": [{"name": "a", "api_key": "k", "engine": "harness", "answer_wait_s": 60,
                        "wall_clock_s": 300,
                        "schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief",
                                       "session": "daily"}]},
                       # no engine: AGENT_ENGINE (legacy) -> its schedules are skipped
                       {"name": "b", "api_key": "kb",
                        "schedules": [{"cron": "0 8 * * *", "prompt": "never"}]}]}
    p = tmp_path / "t.json"
    p.write_text(json.dumps(doc))
    reg = TenantRegistry(p, "env-key")
    a = reg.get_by_name("a")
    entry = a.workflow_entry()
    assert "api_key" not in entry and entry["user"] == "a" and entry["engine"] == "harness"
    assert reg.get_by_name("b").workflow_entry()["engine"] == "legacy"
    cfg = SessionConfig.from_tenant(entry)
    assert (cfg.answer_wait_s, cfg.wall_clock_s) == (60, 300)
    # every knob flows reads is a Tenant field that reaches the entry
    from flows.activities import SESSION_CONFIG_KEYS
    from app.tenants import Tenant
    knobs = dict(zip(SESSION_CONFIG_KEYS, (301, 61, 7, 900)))
    t = Tenant(name="k", api_key="k", **knobs)
    assert {k: t.workflow_entry()[k] for k in SESSION_CONFIG_KEYS} == knobs
    cfg = SessionConfig.from_tenant(t.workflow_entry())
    assert (cfg.wall_clock_s, cfg.answer_wait_s, cfg.max_tasks_per_run,
            cfg.max_history_events) == (301, 61, 7, 900)
    tdoc = reg.tenants_doc()
    assert all("api_key" not in t for t in tdoc["tenants"])
    [e] = entries_from_tenants(tdoc)
    assert (e.tenant, e.user, e.session, e.cron) == ("a", "a", "daily", "0 7 * * *")
    assert SessionConfig.from_tenant(reg.get_by_name("default").workflow_entry()) == SessionConfig()


async def test_example_tenants_load_with_m5_keys():
    doc = json.loads((Path(__file__).parent.parent / "tenants.json.example").read_text())
    from app.tenants import Tenant
    default = Tenant(**{**doc["tenants"][0], "api_key": "x"})
    assert default.answer_wait_s == 86400 and default.schedules[0]["prompt"] == "Morning brief"
