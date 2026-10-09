"""Worker.act() against a scripted FakeGuest over the real Unix-socket
endpoints: text answer, host tool, device tool, redelivery, guest death."""
import asyncio
import json
import socket

import pytest

from protocol.fold import fold, state_hash

from app.host.sides import DeviceCalls
from app.host.stream import DONE, tokens
from app.host.worker import GuestError, Worker, Workers, strip_current_time
from host_fakes import (FakeRegistry, FakeSandbox, FakeTenant, read_jsonl, session,  # noqa: F401
                        token_text, users_root, worker_kwargs)


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
    assert task["body"] == {"input_id": "in_1", "text": "hi", "attachments": []}
    assert guest.acked == guest.seq
    # stored log == guest log; projections; host events; sandbox stopped at settle
    stored = w.sync.read(0)
    assert [e["seq"] for e in stored] == [e["seq"] for e in guest.events]
    assert state_hash(fold(stored)) == state_hash(w.sync.state)
    assert session.read_history() == [{"role": "user", "content": "hi"},
                                      {"role": "assistant", "content": "Hello there world"}]
    kinds = [h["body"].get("kind") or h["body"]["op"]
             for h in w.sync.read_host() if h["type"] in ("vm", "workflow")]
    assert kinds == ["task_signal", "boot", "completed", "destroy"]
    boot = next(h["body"] for h in w.sync.read_host() if h["type"] == "vm")
    assert boot["source"] == "rootfs" and boot["ms"] >= 0 and boot["resume"] is False
    assert not sandbox.is_alive()
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
    assert session.read_history().count({"role": "user", "content": "hi"}) == 1
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
            orig = g._on_input

            def die(msg):
                g.emit("input", {"input_id": msg["body"]["input_id"], "kind": "task",
                                 "text": "x", "dedupe": "new"})
                g.wait_acked(g.seq)
                g.sock.shutdown(socket.SHUT_RDWR)   # the process dies mid-task
                g.sock.close()
                return True
            g._on_input = die

    # max_resumes=0: the M1 behaviour (M3's death resume: test_host_m3.py)
    w = Worker(session, FakeTenant(), FakeRegistry(), Dying([]), device_calls=DeviceCalls(),
               **worker_kwargs(max_resumes=0))
    sink = w.attach()
    result = await w.act({"input_id": "in_1", "text": "hi"})
    sse = await drain(sink)
    assert result["outcome"] == "failed" and "exited before settling" in result["answer"]
    assert any(s.startswith("event: error") for s in sse) and sse[-1] == DONE
    assert session.read_history()[-1]["role"] == "assistant"
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
                  config_for=lambda tenant: {"answer_wait_s": 7},
                  device_calls=DeviceCalls(), **worker_kwargs())
    t = FakeTenant()
    a = reg.get_or_create(t, "s1", FakeRegistry())
    assert a.answer_wait_s == 7                 # config_for over worker_kwargs
    # found by key: the session directory is not opened again
    store.session = None
    assert reg.get_or_create(t, "s1", FakeRegistry()) is a
    await reg.shutdown()
    assert a.closed and reg.get("u/t_s1") is None
    assert strip_current_time("a\n- Current time: {current_time}\nb") == "a\nb"
    assert strip_current_time("WCC prompt. Current time: {current_time}") == "WCC prompt."
    assert strip_current_time("x {current_time} y") == "x  y"


# ---------------------------------------------------------------- M2: hello keys, streaming
@pytest.mark.asyncio
async def test_hello_carries_the_m2_stream_and_limit_keys(session, monkeypatch):
    import app.host.worker as worker_mod

    sandbox = FakeSandbox([{"text": "x"}])
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    hello = w.build_hello()
    assert hello["config"]["params"]["stream"] is True               # D17 default
    limits = hello["config"]["limits"]
    assert limits["context_window_tokens"] == 32768 and limits["reserve_tokens"] == 4096
    assert "token_budget" not in limits                             # a proxy knob only
    assert limits["park_grace_s"] == 30                             # M3
    # the env flip, the window, and the budget the proxy is built with
    monkeypatch.setattr(worker_mod, "LLM_STREAM", False)
    monkeypatch.setattr(worker_mod, "HARNESS_CONTEXT_WINDOW_TOKENS", 8192)
    monkeypatch.setattr(worker_mod, "HARNESS_TOKEN_BUDGET", 1234)
    hello = w.build_hello()
    assert hello["config"]["params"]["stream"] is False
    assert hello["config"]["limits"]["context_window_tokens"] == 8192
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done" and w.proxy.token_budget == 1234
    await w.close()


@pytest.mark.asyncio
async def test_streamed_deltas_reach_the_sink_verbatim_once(session):
    """M2 streaming: the guest's content fragments are the tokens, the
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
    assert session.read_history()[-1] == {"role": "assistant", "content": "Hello, wörld"}
    # second task, LLM_STREAM=false shape: the whole text as one `text` delta
    sandbox.script = [{"text": "Second answer", "deltas": ["Second answer"], "delta_kind": "text"}]
    sink = w.attach()
    result = await w.act({"input_id": "in_2", "text": "again"})
    sse = await drain(sink)
    assert token_text(sse).strip() == "Second answer" and sse[-1] == DONE
    assert sse[1:] == tokens("Second answer") + [DONE]            # the non-streamed shape
    await w.close()
