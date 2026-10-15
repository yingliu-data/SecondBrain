"""Worker.act() against a scripted FakeGuest over the real Unix-socket
endpoints: text answer, host tool, device tool, redelivery, guest death."""
import asyncio
import json
import socket

import pytest

from protocol.fold import fold, state_hash

from app.host.sides import DeviceCalls
from app.host.stream import DONE, tokens
from app.host.worker import Worker, Workers, strip_current_time
from host_fakes import (FakeRegistry, FakeSandbox, FakeTenant, read_jsonl, session,  # noqa: F401
                        token_text, users_root, worker_kwargs)
from eventlog_fakes import conversation


async def drain(q: asyncio.Queue) -> list[str]:
    out = []
    while True:
        item = await asyncio.wait_for(q.get(), 10)
        if item is None:
            return out
        out.append(item)


@pytest.mark.asyncio
async def test_text_answer_streams_legacy_tokens_and_stores_the_log(session):
    sandbox = FakeSandbox([{"text": "Hello there world"}])
    w = Worker(session, FakeTenant(system_prompt="BASE {current_time}"), FakeRegistry(), sandbox,
               device_calls=DeviceCalls(), **worker_kwargs())
    sink = w.attach()
    result = await w.act({"input_id": "in_1", "text": "hi", "ticket_id": "tk_1"})
    sse = await drain(sink)
    assert result["outcome"] == "done" and result["answer"] == "Hello there world"
    assert sse[0].startswith("event: seq\ndata: ")
    assert sse[1:] == tokens("Hello there world") + [DONE]
    # hello: tools with sides and schemas, system block without the time line
    hello = w.last_hello
    names = {t["name"]: t["side"] for t in hello["config"]["tools"]}
    assert names == {"web_search": "host", "set_pose": "host", "get_calendar_events": "device"}
    assert hello["config"]["tools"][0]["schema"]["type"] == "function"
    assert hello["system_block"]["base_prompt"] == "BASE"
    assert hello["config"]["ports"] == {"control": 5000, "bulk": 5001, "llm": 5010, "gateway": 5011}
    assert hello["principal"] == {"user": "u", "tenant": "t"} and hello["resume"] is False
    # the guest's task message carried attachments and got acked
    guest = sandbox.guests[0]
    task = next(c for c in guest.controls if c["type"] == "task")
    # ... and the task's wall clock (the tenant sets none: the Worker's default)
    assert task["body"] == {"input_id": "in_1", "text": "hi", "attachments": [],
                            "wall_clock_s": 10}
    assert hello["config"]["async_tools"] is False
    assert guest.acked == guest.seq
    # stored log == guest log; projections; host events; sandbox stopped at settle
    stored = w.sync.read(0)
    assert [e["seq"] for e in stored] == [e["seq"] for e in guest.events]
    assert state_hash(fold(stored)) == state_hash(w.sync.state)
    assert conversation(session) == [{"role": "user", "content": "hi"},
                                      {"role": "assistant", "content": "Hello there world"}]
    kinds = [h["body"].get("kind") or h["body"]["op"]
             for h in w.sync.read_host() if h["type"] in ("vm", "workflow")]
    # the guest exits right after settle's ack: stopped by the task's end
    # (destroy) when it was already gone then, else seen exiting (link down)
    assert kinds[:3] == ["task_signal", "boot", "completed"] and kinds[3:] in ([], ["destroy"])
    boot = next(h["body"] for h in w.sync.read_host() if h["type"] == "vm")
    assert boot["source"] == "rootfs" and boot["ms"] >= 0 and boot["resume"] is False
    assert sandbox.wait_exit(5)
    await w.close()


@pytest.mark.asyncio
async def test_host_tool_round_trip_and_avatar_command(session):
    reg = FakeRegistry({"web_search": "42 results", "set_pose": json.dumps({"pose": "wave"})})
    sandbox = FakeSandbox([
        {"call": {"id": "call_a", "name": "web_search", "args": {"q": "x"}}},
        {"call": {"id": "call_b", "name": "set_pose", "args": {"pose": "wave"}}},
        {"call": {"id": "call_c", "name": "bash", "args": {"cmd": "ls"}}},
        {"text": "done"},
    ], guest_result="file.txt")
    w = Worker(session, FakeTenant(), reg, sandbox, device_calls=DeviceCalls(), **worker_kwargs())
    sink = w.attach()
    result = await w.act({"input_id": "in_1", "text": "search"})
    sse = await drain(sink)
    assert result["outcome"] == "done"
    assert reg.calls == [("web_search", {"q": "x"}), ("set_pose", {"pose": "wave"})]
    guest = sandbox.guests[0]
    results = {e["body"]["call_id"]: e["body"]["content"]
               for e in guest.events if e["type"] == "tool_result"}
    assert results == {"call_a": "42 results", "call_b": '{"pose": "wave"}', "call_c": "file.txt"}
    assert any(s.startswith("event: avatar_command") for s in sse)
    assert not any(s.startswith("event: tool_call") for s in sse)       # host calls stay on the host
    trace = [t["event"] for t in session.read_trace()]
    assert trace.count("tool_call") == 3 and trace.count("tool_result") == 3
    assert [h["body"]["name"] for h in w.sync.read_host() if h["type"] == "side_call"] == [
        "web_search", "set_pose"]
    await w.close()


@pytest.mark.asyncio
async def test_device_tool_resolved_from_the_tool_result_path(session):
    dc = DeviceCalls()
    sandbox = FakeSandbox([
        {"call": {"id": "call_d", "name": "get_calendar_events", "args": {"day": "today"}}},
        {"text": "You have one event today."},
    ])
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=dc, **worker_kwargs())
    sink = w.attach()
    task = asyncio.ensure_future(w.act({"input_id": "in_1", "text": "calendar?"}))
    # the client sees the legacy tool_call event, then answers via DeviceCalls
    while True:
        item = await asyncio.wait_for(sink.get(), 10)
        if item.startswith("event: tool_call"):
            break
    assert json.loads(item.split("data: ", 1)[1]) == {
        "id": "call_d", "name": "get_calendar_events", "arguments": {"day": "today"}}
    # a stream that attaches while the call is open gets it re-emitted
    while not dc.is_open("call_d"):
        await asyncio.sleep(0.01)
    late = w.attach()
    assert late.get_nowait().startswith("event: tool_call")
    assert dc.resolve("call_d", "[{\"title\": \"Standup\"}]") is True
    result = await task
    rest = await drain(sink)
    assert result["answer"] == "You have one event today."
    assert rest[-1] == DONE
    assert dc.resolve("call_d", "late") is False
    guest = sandbox.guests[0]
    tr = next(e for e in guest.events if e["type"] == "tool_result")
    assert tr["body"]["content"] == "[{\"title\": \"Standup\"}]"
    await w.close()


@pytest.mark.asyncio
async def test_redelivered_input_runs_once(session):
    sandbox = FakeSandbox([{"text": "answer one"}])
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    first = await w.act({"input_id": "in_1", "text": "hi"})
    sink = w.attach()
    again = await w.act({"input_id": "in_1", "text": "hi"})
    sse = await drain(sink)
    assert again == first
    assert sse == tokens("answer one") + [DONE]
    assert sandbox.starts == 1                              # no second guest run
    assert conversation(session).count({"role": "user", "content": "hi"}) == 1
    # a different input is a second run of the guest (fresh log run r0002)
    second = await w.act({"input_id": "in_2", "text": "again"})
    assert second["outcome"] == "done" and sandbox.starts == 2
    assert w.sync.runs() == [1, 2] and w.sync.seen_inputs == {"in_1", "in_2"}
    await w.close()


@pytest.mark.asyncio
async def test_redelivered_while_running_attaches_and_guest_dedupes(session):
    sandbox = FakeSandbox([
        {"call": {"id": "call_d", "name": "get_calendar_events", "args": {}}},
        {"text": "ok"},
    ])
    dc = DeviceCalls()
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=dc, **worker_kwargs())
    t1 = asyncio.ensure_future(w.act({"input_id": "in_1", "text": "hi"}))
    while not dc.is_open("call_d"):
        await asyncio.sleep(0.01)
    t2 = asyncio.ensure_future(w.act({"input_id": "in_1", "text": "hi"}))
    await asyncio.sleep(0.1)
    dc.resolve("call_d", "[]")
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == r2 and r1["outcome"] == "done"
    guest = sandbox.guests[0]
    inputs = [e["body"]["dedupe"] for e in guest.events if e["type"] == "input"]
    assert inputs == ["new", "duplicate"]
    assert sum(1 for e in guest.events if e["type"] == "settle") == 1
    await w.close()


@pytest.mark.asyncio
async def test_guest_death_fails_the_task_with_error_and_done(session):
    class Dying(FakeSandbox):
        def start(self, session_dir):
            super().start(session_dir)
            g = self.guest

            def die(msg):
                g.emit("input", {"input_id": msg["body"]["input_id"], "kind": "task",
                                 "text": "x", "dedupe": "new"})
                g.wait_acked(g.seq)
                g.sock.shutdown(socket.SHUT_RDWR)   # the process dies mid-task
                g.sock.close()
                return True
            g._on_input = die

    # max_resumes=0: no death resume (that is covered in test_host_m3.py)
    w = Worker(session, FakeTenant(), FakeRegistry(), Dying([]), device_calls=DeviceCalls(),
               **worker_kwargs(max_resumes=0))
    sink = w.attach()
    result = await w.act({"input_id": "in_1", "text": "hi"})
    sse = await drain(sink)
    assert result["outcome"] == "failed" and "exited before settling" in result["answer"]
    assert any(s.startswith("event: error") for s in sse) and sse[-1] == DONE
    # The host's failure is in the host log (no history.jsonl projection)
    assert any(h["type"] == "workflow" and h["body"].get("kind") == "completed"
               and h["body"].get("outcome") == "failed" for h in w.sync.read_host())
    await w.close()


@pytest.mark.asyncio
async def test_no_guest_connection_is_a_clean_failure(session):
    class Never(FakeSandbox):
        def start(self, session_dir):
            self.starts += 1

        def is_alive(self):
            return False

    w = Worker(session, FakeTenant(), FakeRegistry(), Never([]), device_calls=DeviceCalls(),
               **worker_kwargs(hello_timeout_s=0.2))
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "failed" and "did not connect" in result["answer"]
    await w.close()


@pytest.mark.asyncio
async def test_workers_registry_and_prompt_stripping(session):
    class Store:
        def __init__(self, session):
            self.session = session

        def get_or_create(self, user, tenant, session_id):
            return self.session if self.session is not None else 1 / 0

    store = Store(session)
    reg = Workers(sandbox_factory=lambda: FakeSandbox([{"text": "x"}]), sessions=store,
                  device_calls=DeviceCalls(), **worker_kwargs())
    t = FakeTenant(answer_wait_s=7)
    a = reg.get_or_create(t, "s1", FakeRegistry())
    assert a.answer_wait_s == 7                 # the tenant's (session_config)
    # found by key: the session directory is not opened again
    store.session = None
    assert reg.get_or_create(t, "s1", FakeRegistry()) is a
    await reg.shutdown()
    assert a.closed and reg.get("u/t/s1") is None
    assert strip_current_time("a\n- Current time: {current_time}\nb") == "a\nb"
    assert strip_current_time("WCC prompt. Current time: {current_time}") == "WCC prompt."
    assert strip_current_time("x {current_time} y") == "x  y"


# ---------------------------------------------------------------- hello keys, streaming
@pytest.mark.asyncio
async def test_hello_carries_the_m2_stream_and_limit_keys(session, monkeypatch):
    import dataclasses

    sandbox = FakeSandbox([{"text": "x"}])
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    hello = w.build_hello()
    assert hello["config"]["params"]["stream"] is True               # default
    limits = hello["config"]["limits"]
    assert limits["context_window_tokens"] == 32768 and limits["reserve_tokens"] == 4096
    assert "token_budget" not in limits                             # a proxy knob only
    assert limits["park_grace_s"] == 30
    # the env flip, the window, and the budget the proxy is built with
    w.settings = dataclasses.replace(w.settings, llm_stream=False, context_window_tokens=8192,
                                     token_budget=1234)
    hello = w.build_hello()
    assert hello["config"]["params"]["stream"] is False
    assert hello["config"]["limits"]["context_window_tokens"] == 8192
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done" and w.proxy.token_budget == 1234
    await w.close()


@pytest.mark.asyncio
async def test_harness_version_skew_is_a_warning_host_event(session):
    from protocol.version import HARNESS_VERSION

    sandbox = FakeSandbox([{"text": "x"}], harness_version="0.0.0-old")
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    assert w.build_hello()["config"]["harness_version"] == HARNESS_VERSION
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done"                              # warn only
    skew = [h["body"] for h in w.sync.read_host() if h["type"] == "harness_skew"]
    assert skew == [{"guest": "0.0.0-old", "host": HARNESS_VERSION}]
    await w.close()


@pytest.mark.asyncio
async def test_streamed_deltas_reach_the_sink_verbatim_once(session):
    """Streaming: the guest's content fragments are the tokens, the
    response is not re-emitted, reasoning never shows; a non-streamed turn's
    whole-text delta goes out word-wise, once."""
    sandbox = FakeSandbox([{"text": "Hello, wörld", "deltas": ["Hel", "lo, wör", "ld"],
                            "reasoning": ["let me think"]}])
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    sink = w.attach()
    result = await w.act({"input_id": "in_1", "text": "hi"})
    sse = await drain(sink)
    assert result["answer"] == "Hello, wörld"
    toks = [json.loads(s.split("data: ", 1)[1])["text"] for s in sse if s.startswith("event: token")]
    assert toks == ["Hel", "lo, wör", "ld"] and sse[-1] == DONE
    assert "think" not in "".join(sse)
    assert conversation(session)[-1] == {"role": "assistant", "content": "Hello, wörld"}
    # second task, LLM_STREAM=false shape: the whole text as one `text` delta
    sandbox.script = [{"text": "Second answer", "deltas": ["Second answer"], "delta_kind": "text"}]
    sink = w.attach()
    result = await w.act({"input_id": "in_2", "text": "again"})
    sse = await drain(sink)
    assert token_text(sse).strip() == "Second answer" and sse[-1] == DONE
    assert sse[1:] == tokens("Second answer") + [DONE]            # the non-streamed shape
    await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_wall", [3, 900])
async def test_the_tenant_wall_clock_is_the_task_s_on_the_direct_path(session, tenant_wall):
    """A tenant wall_clock_s below or above the default (the Worker's
    wall_clock_s, HARNESS_WALL_CLOCK_S) is every task's: in the task
    message (the guest settles `timeout` at it) and the host's wait."""
    sandbox = FakeSandbox([{"text": "ok"}])
    w = Worker(session, FakeTenant(wall_clock_s=tenant_wall), FakeRegistry(), sandbox,
               device_calls=DeviceCalls(), **worker_kwargs())
    assert w.settings.wall_clock_s == 10 and w.default_wall_clock_s() == tenant_wall
    result = await w.act({"input_id": "in_w", "text": "hi"})
    assert result["outcome"] == "done"
    task = next(c for c in sandbox.guests[0].controls if c["type"] == "task")
    assert task["body"]["wall_clock_s"] == tenant_wall
    assert w.last_hello["config"]["limits"]["wall_clock_s"] == tenant_wall
    # a task input that names its own (the workflow's RunTask) wins
    assert w.wall_clock_for({"input_id": "x", "wall_clock_s": 42}) == 42
    await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_value,env_default,expected", [
    (None, False, False), (None, True, True), (True, False, True), (False, True, False)])
async def test_hello_async_tools_is_the_tenant_s_else_the_env_default(
        session, monkeypatch, tenant_value, env_default, expected):
    w = Worker(session, FakeTenant(async_tools=tenant_value), FakeRegistry(),
               FakeSandbox([{"text": "ok"}]), device_calls=DeviceCalls(),
               **worker_kwargs(async_tools=env_default))
    assert w.build_hello()["config"]["async_tools"] is expected
    await w.close()


def test_tenant_async_tools_key_loads():
    from app.tenants import Tenant
    assert Tenant(name="a", api_key="k").async_tools is None
    assert Tenant(name="a", api_key="k", async_tools=True).async_tools is True


def test_flows_outcomes_are_the_settle_outcomes_plus_parked():
    """flows stays independent of protocol, so its Outcome vocabulary is
    spelled twice: run_task returns a guest settle's outcome, or `parked`."""
    from typing import get_args

    from flows.activities import Outcome
    from protocol.events import ENUMS

    assert set(get_args(Outcome)) == set(ENUMS["settle"]["outcome"]) | {"parked"}
