"""POST /chat over the session workflow.

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
from app.host.activities import HostActivities
from app.host.worker import ticket_id_for
from app.host.sandbox.local import LocalSandbox
from app.host.sides import DeviceCalls
from app.host.worker import WORKFLOW, Workers
from app.session.dir_store import DirStore
from app.tenants import TenantRegistry
from host_fakes import FakeRegistry, FakeUpstreamLLM, make_settings, users_root  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")


def short_questions(monkeypatch, *harnesses) -> None:
    """question_timeout_s 1: for the Workers made from now on (app.config,
    read by WorkerSettings.from_config) and those of `harnesses`."""
    import dataclasses
    monkeypatch.setattr("app.config.HARNESS_QUESTION_TIMEOUT_S", 1)
    for d in harnesses:
        d.workers.settings = dataclasses.replace(d.workers.settings, question_timeout_s=1)

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
    """One test's app: tenant `harn`, DirStore, Workers on
    LocalSandbox, the Temporal worker on its own task queue."""

    def __init__(self, env, tmp_path, upstream, monkeypatch, answer_wait_s=ANSWER_WAIT_S,
                 wall_clock_s=120, max_concurrent=5):
        from app.routes import chat, sessions
        self.env = env
        self.upstream = upstream
        self.session_id = "s-" + uuid.uuid4().hex[:8]
        self.ref = SessionRef(user="harn", tenant="harn", session_id=self.session_id)
        entry = {"name": "harn", "api_key": "harn-key"}
        if wall_clock_s is not None:            # None: the tenant does not set it
            entry["wall_clock_s"] = wall_clock_s
        if answer_wait_s is not None:           # None: the tenant does not set it
            entry["answer_wait_s"] = answer_wait_s
        doc = {"tenants": [entry]}
        path = tmp_path / "tenants.json"
        path.write_text(json.dumps(doc))
        self.tenants = TenantRegistry(path, "test-default-key")
        middleware.set_tenant_registry(self.tenants)
        # short: the session's ipc/v.sock_NNNN path must fit AF_UNIX
        self.short = Path(tempfile.mkdtemp(prefix="m5", dir="/tmp"))
        self.store = DirStore(self.short)
        self.workers = Workers(sandbox_factory=lambda: LocalSandbox(GUEST_ROOT),
                               sessions=self.store, max_concurrent=max_concurrent,
                               device_calls=DeviceCalls(),
                               settings=make_settings(abort_wait_s=20, llm_url=upstream.url,
                                                      hello_timeout_s=20, heartbeat_s=1,
                                                      wall_clock_s=120, park_grace_s=30))
        monkeypatch.setattr(chat, "sessions", self.store)
        monkeypatch.setattr(chat, "workers", self.workers)
        monkeypatch.setattr(chat, "HARNESS_KEEPALIVE_S", 0.5)
        sessions.set_sessions(self.store)
        self.registry = FakeRegistry()
        chat.set_registry(self.registry)
        self.acts = HostActivities(self.workers, self.tenants, self.registry)
        app = FastAPI()
        app.include_router(chat.router)
        app.include_router(sessions.router)
        self.app = app
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://test", timeout=120)
        self.queue = "test-" + uuid.uuid4().hex[:8]

    async def start(self, activities=None):
        ok = await temporal.start(activities or self.acts.activities(), None,
                                  connected=self.env.client, queue=self.queue,
                                  max_heartbeat_throttle_interval=timedelta(seconds=0.5),
                                  default_heartbeat_throttle_interval=timedelta(seconds=0.5))
        assert ok and temporal.ready()

    async def chat_in(self, session_id: str, **body) -> str:
        raw, h = self._headers({**body, "session_id": session_id})
        r = await self.http.post("/api/v1/chat", content=raw, headers=h)
        assert r.status_code == 200
        return r.text

    def worker_of(self, session_id: str):
        return self.workers.get(f"harn/harn/{session_id}")

    def restart(self, monkeypatch) -> Workers:
        """A new agent-api process: new Workers (new DeviceCalls) over the
        same session store, used by the routes and the activities from now
        on; the old Workers are left to the caller to stop."""
        from app.routes import chat
        old = self.workers
        self.workers = Workers(sandbox_factory=old.sandbox_factory, sessions=old.sessions,
                               settings=old.settings,
                               **{**old.worker_kwargs, "device_calls": DeviceCalls()})
        self.acts.workers = self.workers
        monkeypatch.setattr(chat, "workers", self.workers)
        return old

    @property
    def worker(self):
        return self.workers.get(f"harn/harn/{self.session_id}")

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

    async def chat_then_disconnect(self, until: str, **body) -> str:
        """POST /chat on the ASGI app itself (httpx's ASGITransport buffers
        the whole body): read the SSE body until it contains `until`, then
        the client goes away (http.disconnect; Starlette cancels the
        response). The text received so far."""
        raw, h = self._headers({"session_id": self.session_id, **body})
        gone = asyncio.Event()
        got: list[str] = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw.encode(), "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.body":
                got.append(msg.get("body", b"").decode())
                if until in "".join(got):
                    gone.set()

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/api/v1/chat",
                 "raw_path": b"/api/v1/chat", "query_string": b"", "root_path": "",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in h.items()],
                 "server": ("test", 80), "client": ("127.0.0.1", 1)}
        await asyncio.wait_for(self.app(scope, receive, send), 60)
        assert gone.is_set(), "the stream ended before the disconnect"
        return "".join(got)

    @property
    def device_calls(self) -> DeviceCalls:
        return self.workers.worker_kwargs["device_calls"]

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
            st = await self.status() if self.env is not None else None
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
                      "driver": "workflow", "outcome": "done",
                      "answer": "Hello through Temporal", "question_id": None, "error": None,
                      "manifest": w.sync.settled["in_t1"]["manifest"],
                      "usage": w.sync.settled["in_t1"]["usage"],
                      "completed_at": ticket["completed_at"]}
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
    short_questions(monkeypatch, h)
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
    # an attaching stream gets the open question again
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
    short_questions(monkeypatch, h)
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
            w = await acts.worker(inp.session)
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


async def _disconnect_mid_task_then_catch_up(d, input_id: str) -> None:
    """The client drops its SSE stream while the task waits for a device
    result: the task continues (the device answers, the task settles) and
    GET /events?after_seq=N returns the rest of the log."""
    d.upstream.responses = [tool_call("call_dc", "get_calendar_events", {}),
                            {"content": "Standup at 9"}]
    body = await d.chat_then_disconnect("event: tool_call", message="calendar?",
                                        input_id=input_id)
    assert "event: done" not in body
    w = d.worker
    assert not any(w._sink_input.get(id(q)) == input_id for q in w.sinks)   # detached
    n = w.sync.last_seq
    assert input_id not in w.sync.settled and w.current_input_id == input_id
    await wait_until(lambda: d.device_calls.is_open("call_dc"), 30, "device call open")
    assert d.device_calls.resolve("call_dc", "[]") is True
    await wait_until(lambda: input_id in w.sync.settled, 60, "settle after the disconnect")
    assert w.sync.settled[input_id]["answer"] == "Standup at 9"
    r = await d.get(f"/events?after_seq={n}")
    assert r.status_code == 200
    rest = r.json()
    assert [e["seq"] for e in rest] == list(range(n + 1, w.sync.last_seq + 1))
    assert [e["body"]["call_id"] for e in rest if e["type"] == "tool_result"] == ["call_dc"]
    assert rest[-1]["type"] == "settle" and rest[-1]["body"]["input_id"] == input_id
    # the same input_id again: the stored answer, no second turn
    again = await d.chat(message="calendar?", input_id=input_id)
    assert "Standup at 9" in token_text(again) and sse_events(again)[-1] == ("done", {})
    assert len(d.upstream.requests) == 2


async def test_client_disconnect_mid_task_workflow_path(h):
    await h.start()
    await _disconnect_mid_task_then_catch_up(h, "in_dc_wf")
    await h.wait_state("idle")
    [ticket] = h.tickets()
    assert ticket["outcome"] == "done" and ticket["answer"] == "Standup at 9"


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


async def test_unreachable_temporal_answers_503_never_the_direct_path(tmp_path, users_root,
                                                                      upstream, monkeypatch):
    """TEMPORAL_ADDRESS configured but Temporal unreachable: the workflow
    path is still the one (configuration, not liveness); POST /chat waits
    TEMPORAL_READY_WAIT_S, then 503 + Retry-After, and no Worker is made."""
    from app.routes import chat
    monkeypatch.setattr("app.host.temporal.TEMPORAL_CONNECT_TIMEOUT_S", 2)
    monkeypatch.setattr(chat, "TEMPORAL_READY_WAIT_S", 0.3)
    d = Harness(None, tmp_path, upstream, monkeypatch)
    try:
        assert await temporal.start([], None, address="127.0.0.1:1") is False
        assert temporal.configured() and not temporal.ready()
        raw, hd = d._headers({"session_id": d.session_id, "message": "hi"})
        r = await d.http.post("/api/v1/chat", content=raw, headers=hd)
        assert r.status_code == 503 and r.headers["retry-after"]
        assert d.worker is None and not upstream.requests
    finally:
        await temporal.stop()
        await d.close()
    assert not temporal.configured()
    assert await temporal.start([], None, address="") is False
    assert not temporal.configured()


async def test_tenant_workflow_entry_and_doc(tmp_path):
    from flows.activities import SessionConfig
    from flows.schedules import entries_from_tenants
    doc = {"tenants": [{"name": "a", "api_key": "k", "engine": "harness", "answer_wait_s": 60,
                        "wall_clock_s": 300,
                        "schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief",
                                       "session": "daily"}]},
                       # an `engine` is ignored, every tenant's schedules run
                       {"name": "b", "api_key": "kb", "engine": "legacy",
                        "schedules": [{"cron": "0 8 * * *", "prompt": "also"}]}]}
    p = tmp_path / "t.json"
    p.write_text(json.dumps(doc))
    reg = TenantRegistry(p, "env-key")
    a = reg.get_by_name("a")
    entry = a.workflow_entry()
    assert "api_key" not in entry and entry["user"] == "a" and "engine" not in entry
    assert "engine" not in reg.get_by_name("b").workflow_entry()
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
    e, eb = entries_from_tenants(tdoc)
    assert (e.tenant, e.user, e.session, e.cron) == ("a", "a", "daily", "0 7 * * *")
    assert (eb.tenant, eb.cron) == ("b", "0 8 * * *")
    assert SessionConfig.from_tenant(reg.get_by_name("default").workflow_entry()) == SessionConfig()


async def test_example_tenants_load_with_m5_keys():
    doc = json.loads((Path(__file__).parent.parent / "tenants.json.example").read_text())
    from app.tenants import Tenant
    default = Tenant(**{**doc["tenants"][0], "api_key": "x"})
    assert default.answer_wait_s == 86400 and default.schedules[0]["prompt"] == "Morning brief"


# --------------------------------------------------------------------------- #
# the direct path (no Temporal) handles a question park like the workflow


@pytest_asyncio.fixture(loop_scope="session")
async def direct(tmp_path, users_root, upstream, monkeypatch):
    harness = Harness(None, tmp_path, upstream, monkeypatch, answer_wait_s=4)
    yield harness
    await harness.close()


async def _direct_question_answered(d, monkeypatch, input_id):
    short_questions(monkeypatch, d)
    d.upstream.responses = [tool_call("call_q", "question", {"text": "Which colour?",
                                                            "options": ["red", "blue"]}),
                            {"content": "You picked blue"}]
    assert not temporal.configured()
    first = asyncio.ensure_future(d.chat(message="ask me", input_id=input_id))
    w = await wait_until(lambda: d.worker is not None and d.worker.question_park is not None
                         and d.worker, 30, "question park")
    assert w.answer_wait_s == 4 and w.last_hello["config"]["limits"]["answer_wait_s"] == 4
    qid = w.question_park["question_id"]
    assert w.question_park["input_id"] == input_id and w.vm_state == "parked"
    assert not first.done()                      # the stream stays open while parked
    second = await d.chat(message="blue", answer_to=qid, input_id=input_id + "_a")
    body = await asyncio.wait_for(first, 60)
    for b in (body, second):
        assert sse_events(b)[-1] == ("done", {})
        assert not any(e == "error" for e, _ in sse_events(b))
        assert "You picked blue" in token_text(b)
    assert any(e == "question" and dd["question_id"] == qid for e, dd in sse_events(body))
    assert w.sync.settled[input_id]["outcome"] == "done"
    assert d.upstream.requests[1]["messages"][-1]["content"] == "blue"
    assert not d.host_events("answer_timeout")


async def test_direct_path_question_parks_then_answer_resumes(direct, monkeypatch):
    await _direct_question_answered(direct, monkeypatch, "in_dq1")


async def test_direct_path_answer_wait_escalates(direct, monkeypatch):
    short_questions(monkeypatch, direct)
    direct.upstream.responses = [tool_call("call_q", "question", {"text": "Still there?"}),
                                 {"content": "never"}]
    t0 = time.monotonic()
    body = await asyncio.wait_for(direct.chat(message="ask", input_id="in_dq2"), 90)
    assert time.monotonic() - t0 >= 4             # waited answer_wait_s
    events = sse_events(body)
    assert events[-1] == ("done", {})
    assert any(e == "error" and d["outcome"] == "escalated" for e, d in events)
    w = direct.worker
    # the guest wrote the settle itself after wake + abort{escalated}
    assert w.sync.settled["in_dq2"]["outcome"] == "escalated"
    assert [b["question_id"] for b in direct.host_events("answer_timeout")]
    assert [b["reason"] for b in direct.host_events("abort")] == ["escalated"]
    assert len(direct.upstream.requests) == 1


async def test_client_disconnect_mid_task_direct_path(direct):
    assert not temporal.configured()
    await _disconnect_mid_task_then_catch_up(direct, "in_dc_direct")


async def test_temporal_worker_exit_is_503_not_the_direct_path(h, monkeypatch):
    """The Temporal worker exits on its own: not ready (503 + Retry-After)
    until the supervisor restarts it; the direct path is never taken."""
    from app.routes import chat
    monkeypatch.setattr(chat, "TEMPORAL_READY_WAIT_S", 0.3)
    await h.start()
    assert temporal.configured() and temporal.ready()
    worker, task = temporal.runtime.worker, temporal.runtime.worker_task
    temporal.runtime.worker_exited(task)                 # as if the worker died
    try:
        assert temporal.configured() and not temporal.ready()
        raw, hd = h._headers({"session_id": h.session_id, "message": "hi"})
        r = await h.http.post("/api/v1/chat", content=raw, headers=hd)
        assert r.status_code == 503 and r.headers["retry-after"]
        assert h.worker is None and not h.upstream.requests
        r = await h.post("/abort")
        assert r.status_code == 404                   # no session dir yet
        h.store.get_or_create("harn", "harn", h.session_id)
        r = await h.post("/abort")
        assert r.status_code == 503
    finally:
        await worker.shutdown()
        await task


async def test_direct_answer_to_another_question_while_parked_is_refused(direct, monkeypatch):
    short_questions(monkeypatch, direct)
    direct.upstream.responses = [tool_call("call_q", "question", {"text": "Which?"}),
                                 {"content": "ok"}]
    first = asyncio.ensure_future(direct.chat(message="ask", input_id="in_dq4"))
    w = await wait_until(lambda: direct.worker is not None
                         and direct.worker.question_park is not None and direct.worker,
                         30, "question park")
    body = await direct.chat(message="x", answer_to="question_other", input_id="in_dq4_x")
    assert ("error", {"class": "fatal",
                      "message": "No question is waiting for this answer."}) in sse_events(body)
    assert w.vm_state == "parked" and w.question_park is not None
    qid = w.question_park["question_id"]
    await direct.chat(message="fine", answer_to=qid, input_id="in_dq4_a")
    assert sse_events(await asyncio.wait_for(first, 60))[-1] == ("done", {})


async def test_direct_redelivery_while_parked_on_a_question_does_not_wake(direct, monkeypatch):
    """A redelivered input (act() again) or any control but the answer and
    the abort is held without a wake: a woken guest would not park again
    and the answer wait would be lost."""
    short_questions(monkeypatch, direct)
    direct.upstream.responses = [tool_call("call_q", "question", {"text": "Which?"}),
                                 {"content": "ok"}]
    first = asyncio.ensure_future(direct.chat(message="ask", input_id="in_dq5"))
    w = await wait_until(lambda: direct.worker is not None
                         and direct.worker.question_park is not None and direct.worker,
                         30, "question park")
    again = asyncio.ensure_future(w.act({"input_id": "in_dq5", "text": "ask"}))
    await asyncio.sleep(1.0)
    assert w.vm_state == "parked" and w.question_park is not None and not again.done()
    qid = w.question_park["question_id"]
    await direct.chat(message="fine", answer_to=qid, input_id="in_dq5_a")
    assert sse_events(await asyncio.wait_for(first, 60))[-1] == ("done", {})
    assert (await asyncio.wait_for(again, 10))["outcome"] == "done"
    assert not direct.host_events("answer_timeout")


async def test_direct_parked_sessions_do_not_hold_the_global_slots(tmp_path, users_root,
                                                                  upstream, monkeypatch):
    """The Workers' admission caps running tasks at 5; a task parked on a
    question (up to answer_wait_s, here an hour) gives its slot back, so
    six parked sessions never block a seventh session's chat."""
    short_questions(monkeypatch)
    d = Harness(None, tmp_path, upstream, monkeypatch, answer_wait_s=3600)
    assert d.workers.admission.max_concurrent == 5
    n = 6
    d.upstream.responses = [tool_call(f"call_q{i}", "question", {"text": f"Q{i}?"})
                            for i in range(n)] + [{"content": "free to answer"}]
    workers, parked = [], []
    try:
        for i in range(n):
            sid = f"{d.session_id}-p{i}"
            parked.append(asyncio.ensure_future(d.chat(session_id=sid, message="ask",
                                                       input_id=f"in_p{i}")))
            workers.append(await wait_until(
                lambda: (w := d.workers.get(f"harn/harn/{sid}")) is not None
                and w.parked_on(f"in_p{i}") and w, 30, f"park {i}"))
        body = await asyncio.wait_for(d.chat(session_id=f"{d.session_id}-new",
                                             message="hi", input_id="in_new"), 60)
        assert sse_events(body)[-1] == ("done", {})
        assert token_text(body).strip() == "free to answer"
        # still parked, their streams open
        assert all(w.parked_on(f"in_p{i}") for i, w in enumerate(workers))
        assert not any(p.done() for p in parked)
    finally:
        for p in parked:
            p.cancel()
        await d.close()


# --------------------------------------------------------------------------- #
# one task cap (Workers' admission) on both paths; one wall clock on both paths


async def _cap_of_one(d, monkeypatch, workflow: bool) -> None:
    """max_concurrent 1: a task parked on a question holds no slot (another
    session's task runs), a running task does (the next one waits for it:
    it asks the model only after the first task settled)."""
    short_questions(monkeypatch, d)
    assert d.workers.admission.max_concurrent == 1
    sa, sb, sc, sd = (f"{d.session_id}-{x}" for x in "abcd")
    d.upstream.responses = [tool_call("call_q", "question", {"text": "Which?"}),
                            {"content": "B done"}, {"content": "A done"},
                            tool_call("call_s", "bash", {"command": "sleep 2; echo slept"}),
                            {"content": "C done"}, {"content": "D done"}]
    first = asyncio.ensure_future(d.chat_in(sa, message="ask", input_id="in_ca"))
    wa = await wait_until(lambda: (w := d.worker_of(sa)) is not None and w.parked_on("in_ca")
                          and w, 60, "A parks")
    assert d.workers.admission.free == 1                     # the park gave its slot back
    body = await asyncio.wait_for(d.chat_in(sb, message="hi", input_id="in_cb"), 60)
    assert token_text(body).strip() == "B done"
    qid = wa.question_park["question_id"]
    await d.chat_in(sa, message="x", answer_to=qid, input_id="in_ca_a")
    assert token_text(await asyncio.wait_for(first, 60)).strip() == "A done"
    if workflow:
        await wait_until(lambda: d.workers.admission.free == 1, 30, "A's slot back")
    c = asyncio.ensure_future(d.chat_in(sc, message="slow", input_id="in_cc"))
    wc = await wait_until(lambda: (w := d.worker_of(sc)) is not None and any(
        e["type"] == "tool_call" for e in w.sync.read(0)) and w, 60, "C's tool runs")
    dd = asyncio.ensure_future(d.chat_in(sd, message="next", input_id="in_cd"))
    await asyncio.sleep(1.0)
    wd = d.worker_of(sd)
    # D waits for C's slot: no task of it runs, nothing went to its guest
    assert wd is None or (wd.current_input_id is None and not any(
        e["type"] == "input" for e in wd.sync.read(0)))
    assert not dd.done() and d.workers.admission.free == 0
    assert token_text(await asyncio.wait_for(c, 60)).strip() == "C done"
    assert token_text(await asyncio.wait_for(dd, 60)).strip() == "D done"
    assert wc.sync.settled["in_cc"]["answer"] == "C done"
    # D asked the model only after C settled: it got the 6th answer, not C's
    assert d.worker_of(sd).sync.settled["in_cd"]["answer"] == "D done"
    await wait_until(lambda: d.workers.admission.free == 1, 30, "every slot back")


async def test_task_cap_on_the_direct_path(tmp_path, users_root, upstream, monkeypatch):
    d = Harness(None, tmp_path, upstream, monkeypatch, answer_wait_s=3600, max_concurrent=1)
    try:
        await _cap_of_one(d, monkeypatch, workflow=False)
    finally:
        await d.close()


async def test_task_cap_on_the_workflow_path(tenv, tmp_path, users_root, upstream, monkeypatch):
    d = Harness(tenv, tmp_path, upstream, monkeypatch, answer_wait_s=3600, max_concurrent=1)
    try:
        await d.start()
        await _cap_of_one(d, monkeypatch, workflow=True)
    finally:
        for sid in (f"{d.session_id}-{x}" for x in "abcd"):
            try:
                await session_handle(tenv.client, SessionRef("harn", "harn", sid)).terminate("done")
            except Exception:  # noqa: BLE001
                pass
        await d.close()


def _run_task_started(d, sid: str, input_id: str):
    """The Worker of `sid` once its run_task for `input_id` started (the
    activity's host event), else None."""
    w = d.worker_of(sid)
    if w is None:
        return None
    return w if any(h["type"] == "workflow" and h["body"].get("kind") == "activity_start"
                    and h["body"].get("activity") == "run_task"
                    and h["body"].get("input_id") == input_id
                    for h in w.sync.read_host()) else None


async def test_workflow_task_waits_past_its_wall_clock_for_a_slot(tenv, tmp_path, users_root,
                                                                  upstream, monkeypatch):
    """max_concurrent 1: B's run_task (wall clock 3 s) waits for A's slot
    longer than its wall clock + grace and is not timed out: its time limit
    counts from its start, run_task's Temporal timeouts leave room for the
    queue (wall + grace + max_queue_wait_s). B then runs and completes.
    (Real time: the test server does not skip time while activities run, so
    the grace is patched down from its 30 s on both sides: the host's
    backstop and run_task's Temporal timeouts.)"""
    import flows.activities
    import app.host.worker as worker_mod
    WALL_CLOCK_GRACE_S = 2
    monkeypatch.setattr(flows.activities, "WALL_CLOCK_GRACE_S", WALL_CLOCK_GRACE_S)
    monkeypatch.setattr(worker_mod, "WALL_CLOCK_GRACE_S", WALL_CLOCK_GRACE_S)
    d = Harness(tenv, tmp_path, upstream, monkeypatch, wall_clock_s=90, max_concurrent=1)
    sa, sb = f"{d.session_id}-a", f"{d.session_id}-b"
    b_wall = 3
    hold = b_wall + WALL_CLOCK_GRACE_S + 4              # A holds the slot this long
    d.upstream.responses = [tool_call("call_s", "bash", {"command": f"sleep {hold}; echo slept"}),
                            {"content": "A done"}, {"content": "B done"}]
    acts, timeouts = d.acts, {}

    @activity.defn(name="run_task")
    async def run_task(inp: RunTask) -> RunResult:
        timeouts[inp.input_id] = (inp.wall_clock_s, activity.info().start_to_close_timeout)
        return await acts.run_task(inp)
    try:
        await d.start([acts.prepare_vm, run_task, acts.park_vm, acts.deliver])
        a = asyncio.ensure_future(d.chat_in(sa, message="slow", input_id="in_qa"))
        await wait_until(lambda: (w := d.worker_of(sa)) is not None and any(
            e["type"] == "tool_call" for e in w.sync.read(0)), 60, "A's tool runs")
        d.tenants.get_by_name("harn").wall_clock_s = b_wall     # B's task signal carries it
        t0 = time.monotonic()
        b = asyncio.ensure_future(d.chat_in(sb, message="next", input_id="in_qb"))
        wb = await wait_until(lambda: _run_task_started(d, sb, "in_qb"), 30, "B's run_task")
        assert timeouts["in_qb"][0] == b_wall
        assert timeouts["in_qb"][1].total_seconds() > b_wall + WALL_CLOCK_GRACE_S + 60
        assert wb.task_for("in_qb") is not None and wb.current_input_id is None  # queued
        assert token_text(await asyncio.wait_for(a, hold + 60)).strip() == "A done"
        waited = time.monotonic() - t0
        assert waited > b_wall + WALL_CLOCK_GRACE_S
        body = await asyncio.wait_for(b, 60)
        assert token_text(body).strip() == "B done" and sse_events(body)[-1] == ("done", {})
        assert wb.sync.settled["in_qb"]["outcome"] == "done"
        [inp] = [e["body"] for e in wb.sync.read(0) if e["type"] == "input"]
        assert inp["input_id"] == "in_qb" and inp["wall_clock_s"] == b_wall
        assert not [h for h in wb.sync.read_host() if h["type"] == "workflow"
                    and h["body"].get("kind") == "dropped"]
        [ticket] = [json.loads(p.read_text()) for p in wb.session.tickets.glob("ticket_*.json")]
        assert ticket["outcome"] == "done"
        await wait_until(lambda: d.workers.admission.free == 1, 30, "every slot back")
    finally:
        for sid in (sa, sb):
            try:
                await session_handle(tenv.client, SessionRef("harn", "harn", sid)).terminate("done")
            except Exception:  # noqa: BLE001
                pass
        await d.close()


async def test_workflow_cancelled_queued_task_never_starts(tenv, tmp_path, users_root,
                                                           upstream, monkeypatch):
    """max_concurrent 1: B's run_task waits for A's slot when B's workflow
    is aborted. B's queued task is dropped (host event workflow{dropped};
    deliver(aborted) or the activity's cancel): no abort to a guest, nothing of B ever reaches its
    guest or the model, also after A gave the slot back; B ends aborted."""
    from flows.client import signal_abort
    d = Harness(tenv, tmp_path, upstream, monkeypatch, max_concurrent=1)
    sa, sb = f"{d.session_id}-a", f"{d.session_id}-b"
    d.upstream.responses = [tool_call("call_s", "bash", {"command": "sleep 4; echo slept"}),
                            {"content": "A done"}, {"content": "B must not run"}]
    try:
        await d.start()
        a = asyncio.ensure_future(d.chat_in(sa, message="slow", input_id="in_xa"))
        await wait_until(lambda: (w := d.worker_of(sa)) is not None and any(
            e["type"] == "tool_call" for e in w.sync.read(0)), 60, "A's tool runs")
        b = asyncio.ensure_future(d.chat_in(sb, message="next", input_id="in_xb"))
        wb = await wait_until(lambda: _run_task_started(d, sb, "in_xb"), 30, "B's run_task")
        assert wb.current_input_id is None
        await signal_abort(tenv.client, SessionRef("harn", "harn", sb), "user cancelled")
        body = await asyncio.wait_for(b, 60)
        events = sse_events(body)
        assert events[-1] == ("done", {})
        assert any(e == "error" and dd["outcome"] == "aborted" for e, dd in events)
        assert wb.task_for("in_xb") is None
        hosts = [h["body"] for h in wb.sync.read_host() if h["type"] == "workflow"]
        # dropped once: by deliver(aborted) or the activity's cancel, first wins
        [dropped] = [h for h in hosts if h.get("kind") == "dropped"]
        assert dropped["input_id"] == "in_xb" and dropped["reason"] in ("aborted", "cancelled")
        assert not any(h.get("kind") in ("abort", "task_signal") for h in hosts)
        assert token_text(await asyncio.wait_for(a, 60)).strip() == "A done"
        await wait_until(lambda: d.workers.admission.free == 1, 30, "A's slot back")
        await asyncio.sleep(1.5)
        # B never ran: no input on its guest, the model asked twice (A's turns)
        assert not any(e["type"] == "input" for e in wb.sync.read(0))
        assert "in_xb" not in wb.sync.settled and len(d.upstream.requests) == 2
        assert d.workers.admission.free == 1
        [ticket] = [json.loads(p.read_text()) for p in wb.session.tickets.glob("ticket_*.json")]
        assert ticket["outcome"] == "aborted"
    finally:
        for sid in (sa, sb):
            try:
                await session_handle(tenv.client, SessionRef("harn", "harn", sid)).terminate("done")
            except Exception:  # noqa: BLE001
                pass
        await d.close()


async def _wall_clock_case(d, tenant_wall: int, input_id: str) -> dict:
    """A tenant wall_clock_s below (3) or above (900) the default (the
    Workers' wall_clock_s, 120): the task message carries it (logged on the
    guest's input event); below, the guest settles `timeout` at it."""
    if tenant_wall < 120:
        d.upstream.responses = [tool_call("call_w", "bash", {"command": "sleep 30"}),
                                {"content": "too late"}]
    else:
        d.upstream.responses = [{"content": "in time"}]
    t0 = time.monotonic()
    body = await asyncio.wait_for(d.chat(message="go", input_id=input_id), 90)
    w = d.worker
    [inp] = [e["body"] for e in w.sync.read(0)
             if e["type"] == "input" and e["body"]["input_id"] == input_id]
    assert inp["wall_clock_s"] == tenant_wall
    settle = w.sync.settled[input_id]
    if tenant_wall < 120:
        assert settle["outcome"] == "timeout" and time.monotonic() - t0 < 25
        assert any(e == "error" and dd["outcome"] == "timeout" for e, dd in sse_events(body))
    else:
        assert settle["outcome"] == "done" and token_text(body).strip() == "in time"
    return settle


@pytest.mark.parametrize("tenant_wall", [3, 900])
async def test_tenant_wall_clock_on_the_direct_path(tmp_path, users_root, upstream, monkeypatch,
                                                    tenant_wall):
    d = Harness(None, tmp_path, upstream, monkeypatch, wall_clock_s=tenant_wall)
    try:
        await _wall_clock_case(d, tenant_wall, "in_wd")
        [ticket] = d.tickets()
        assert ticket["driver"] == "direct" and ticket["outcome"] == d.worker.sync.settled[
            "in_wd"]["outcome"]
    finally:
        await d.close()


@pytest.mark.parametrize("tenant_wall", [3, 900])
async def test_tenant_wall_clock_on_the_workflow_path(tenv, tmp_path, users_root, upstream,
                                                      monkeypatch, tenant_wall):
    """The same as the direct path; run_task's Temporal timeouts are a
    safety net past it: the task's wall clock + WALL_CLOCK_GRACE_S +
    max_queue_wait_s (HARNESS_MAX_QUEUE_WAIT_S), the same for both."""
    from flows.activities import WALL_CLOCK_GRACE_S
    monkeypatch.setattr("app.host.hello.HARNESS_MAX_QUEUE_WAIT_S", 700)
    d = Harness(tenv, tmp_path, upstream, monkeypatch, wall_clock_s=tenant_wall)
    acts, seen = d.acts, []

    @activity.defn(name="run_task")
    async def run_task(inp: RunTask) -> RunResult:
        info = activity.info()
        seen.append((inp.wall_clock_s, info.start_to_close_timeout,
                     info.schedule_to_close_timeout))
        return await acts.run_task(inp)
    try:
        await d.start([acts.prepare_vm, run_task, acts.park_vm, acts.deliver])
        await _wall_clock_case(d, tenant_wall, "in_ww")
        net = timedelta(seconds=tenant_wall + WALL_CLOCK_GRACE_S + 700)
        assert seen == [(tenant_wall, net, net)]
        await d.wait_state("idle")
        [ticket] = d.tickets()
        assert ticket["driver"] == "workflow" and ticket["outcome"] == d.worker.sync.settled[
            "in_ww"]["outcome"]
    finally:
        await d.close()


# --------------------------------------------------------------------------- #
# an agent-api restart: no model turn runs twice, parks survive


async def test_run_task_retry_in_a_new_process_resumes_the_run(h, tmp_path, monkeypatch):
    """The process dies mid-task (its Workers go away, the guest is killed,
    nothing parks); Temporal retries run_task in a new process: the input is
    unsettled in the current run, so that run is resumed (hello{resume:
    true} + the log return), not a new one opened: the running tool is lost
    and not run again, the logged turn is not asked again, one settle."""
    from test_host_restart import assert_resumed_once, of_type
    marker = tmp_path / "ran.txt"
    h.upstream.responses = [tool_call("call_n", "bash",
                                      {"command": f"echo ran >> {marker}; sleep 60"}),
                            {"content": "finished once"}]
    acts = h.acts

    @activity.defn(name="run_task")
    async def dying_run_task(inp: RunTask) -> RunResult:
        if activity.info().attempt == 1:
            w = await acts.worker(inp.session)
            w.start_task({"input_id": inp.input_id, "text": inp.text, "kind": inp.kind,
                          "driver": WORKFLOW})
            await wait_until(lambda: marker.exists() and any(
                b["call_id"] == "call_n" and b["state"] == "running"
                for b in of_type(w, "op_state")), 30, "the tool runs")
            old = h.restart(monkeypatch)
            await old.shutdown(park_s=0)            # a crash: the guest dies unparked
            raise ApplicationError("simulated process death")
        return await acts.run_task(inp)

    await h.start([acts.prepare_vm, dying_run_task, acts.park_vm, acts.deliver])
    await h.chat(message="slow one", input_id="in_nr")      # its stream ends with the old process
    await h.wait_state("idle", 60)
    w = h.worker
    assert w.sync.settled["in_nr"]["answer"] == "finished once"
    assert_resumed_once(w, "in_nr", "call_n", marker, h.upstream)
    [ticket] = h.tickets()
    assert ticket["outcome"] == "done" and ticket["answer"] == "finished once"
    retry = [b for b in h.host_events("activity_start") if b["activity"] == "run_task"]
    assert [r["attempt"] for r in retry] == [2]             # attempt 1 was the stand-in
    # the client re-sends the input: the stored answer
    again = await h.chat(message="slow one", input_id="in_nr")
    assert "finished once" in token_text(again) and len(h.upstream.requests) == 2


async def test_workflow_path_answer_wait_defaults_to_the_session_config(
        tenv, tmp_path, users_root, upstream, monkeypatch):
    """A tenant without answer_wait_s: the workflow path's guest gets
    SessionConfig's default (86400) in its hello."""
    harness = Harness(tenv, tmp_path, upstream, monkeypatch, answer_wait_s=None)
    try:
        upstream.responses = [{"content": "hi"}]
        await harness.start()
        body = await harness.chat(message="x", input_id="in_def")
        assert sse_events(body)[-1] == ("done", {})
        assert harness.worker.last_hello["config"]["limits"]["answer_wait_s"] == 86400
    finally:
        await harness.close()


async def _park_then_restart(tmp_path, upstream, monkeypatch, answer_wait_s, input_id):
    """A direct-path task parks on a question; agent-api stops (graceful:
    its guest is killed) and starts again: the parked question is restored
    at startup (load_question_parks). (harness, the new Worker, question_id,
    the park's monotonic time)"""
    short_questions(monkeypatch)
    d = Harness(None, tmp_path, upstream, monkeypatch, answer_wait_s=answer_wait_s)
    upstream.responses = [tool_call("call_q", "question", {"text": "Which colour?",
                                                          "options": ["red", "blue"]}),
                          {"content": "You picked blue"}]
    first = asyncio.ensure_future(d.chat(message="ask me", input_id=input_id))
    w1 = await wait_until(lambda: d.worker is not None and d.worker.question_park is not None
                          and d.worker, 30, "question park")
    t_park = time.monotonic()
    qid = w1.question_park["question_id"]
    old = d.restart(monkeypatch)
    await old.shutdown()
    await asyncio.wait_for(first, 30)                   # the old process's stream ended
    info = json.loads(w1.parked_file.read_text())
    assert info["question"] == {"input_id": input_id, "question_id": qid}
    assert info["driver"] == "direct" and info["answer_wait_s"] == answer_wait_s
    [w2] = await d.workers.load_question_parks(d.tenants.all_tenants(), d.registry)
    assert w2.vm_state == "parked" and w2.question_park == {"input_id": input_id,
                                                            "question_id": qid}
    assert 0 < w2.answer_left_s() <= answer_wait_s
    return d, w2, qid, t_park


async def test_direct_question_park_survives_a_restart_and_the_answer_completes(
        tmp_path, users_root, upstream, monkeypatch):
    d, w, qid, _ = await _park_then_restart(tmp_path, upstream, monkeypatch, 3600, "in_rq1")
    try:
        body = await asyncio.wait_for(d.chat(message="blue", answer_to=qid,
                                             input_id="in_rq1_a"), 60)
        events = sse_events(body)
        assert events[-1] == ("done", {}) and not any(e == "error" for e, _ in events)
        assert "You picked blue" in token_text(body)
        assert w.sync.settled["in_rq1"]["outcome"] == "done"
        assert len(upstream.requests) == 2
        assert upstream.requests[1]["messages"][-1]["content"] == "blue"
        assert w.sync.runs() == [1]
        assert not d.host_events("answer_timeout")
        # the client re-sends the task: its stored answer
        again = await d.chat(message="ask me", input_id="in_rq1")
        assert "You picked blue" in token_text(again) and len(upstream.requests) == 2
    finally:
        await d.close()


async def test_direct_question_park_restart_resumes_the_answer_wait(
        tmp_path, users_root, upstream, monkeypatch):
    """No answer: the restored driver escalates when the rest of the
    answer wait (from the park, not from the restart) has run out."""
    d, w, qid, t_park = await _park_then_restart(tmp_path, upstream, monkeypatch, 6, "in_rq2")
    try:
        await wait_until(lambda: "in_rq2" in w.sync.settled, 60, "escalated settle")
        elapsed = time.monotonic() - t_park
        assert w.sync.settled["in_rq2"]["outcome"] == "escalated"
        assert 5 <= elapsed < 20
        assert [b["question_id"] for b in d.host_events("answer_timeout")] == [qid]
        assert len(upstream.requests) == 1
    finally:
        await d.close()


async def test_direct_new_input_waits_for_a_restored_question_park(
        tmp_path, users_root, upstream, monkeypatch):
    """A new input after the restart does not wake the guest behind the
    restored answer wait: it waits for that task (answered here) and then
    runs as its own task."""
    d, w, qid, _ = await _park_then_restart(tmp_path, upstream, monkeypatch, 3600, "in_rq3")
    try:
        upstream.responses = upstream.responses + [{"content": "next answer"}]
        nxt = asyncio.ensure_future(d.chat(message="next", input_id="in_rq3_next"))
        await asyncio.sleep(1.0)
        assert w.vm_state == "parked" and w.question_park is not None and not nxt.done()
        await asyncio.wait_for(d.chat(message="blue", answer_to=qid, input_id="in_rq3_a"), 60)
        body = await asyncio.wait_for(nxt, 60)
        assert "next answer" in token_text(body)
        assert [s["input_id"] for s in (e["body"] for e in w.sync.read(0)
                                        if e["type"] == "settle")] == ["in_rq3", "in_rq3_next"]
    finally:
        await d.close()


def test_flows_outcomes_match_protocol_settle_outcomes():
    """flows.activities.Outcome is the settle outcomes (protocol.events) plus
    the workflow-only "parked": the two lists cannot drift apart."""
    from typing import get_args

    from flows.activities import Outcome
    from protocol.events import ENUMS

    assert set(get_args(Outcome)) - {"parked"} == set(ENUMS["settle"]["outcome"])
    assert "parked" in get_args(Outcome)
