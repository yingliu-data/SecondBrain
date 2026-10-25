"""Idle Worker eviction (Workers.evict_idle / the reaper): an idle Worker is
closed and forgotten (its threads released), rebuilt from disk on the next
request; a parked session with an adopted device call or a question park
stays in memory."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from protocol.fold import checkpoint_body

from app.host.sides import DeviceCalls
from app.host.state_sync import StateSync
from app.host.worker import Workers
from app.session.dir_store import DirStore
from app.session.ids import session_key
from host_fakes import FakeRegistry, FakeSandbox, FakeTenant, users_root, worker_kwargs  # noqa: F401
from test_host_state_sync import ev, text_task

KEY = session_key("u", "t", "s1")


def _threads_of(key: str) -> list[str]:
    return [t.name for t in threading.enumerate() if key in t.name and t.is_alive()]


async def _wait(pred, timeout=10.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        assert loop.time() - t0 < timeout, "timed out"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_an_idle_worker_is_evicted_and_rebuilt_on_the_next_request(tmp_path):
    store = DirStore(tmp_path / "sessions")
    workers = Workers(sandbox_factory=lambda: FakeSandbox([{"text": "first"}]), sessions=store,
                      evict_s=0.3, device_calls=DeviceCalls(), **worker_kwargs())
    tenant = FakeTenant()
    try:
        w = await workers.open(tenant, "s1", FakeRegistry())
        assert (await w.act({"input_id": "in_1", "text": "hi"}))["answer"] == "first"
        assert w._accept_thread.is_alive() and _threads_of(KEY)
        # the reaper drops it once idle for evict_s (the guest exited at settle)
        await _wait(lambda: workers.get(KEY) is None)
        assert w.closed
        await _wait(lambda: not _threads_of(KEY))
        assert not w._accept_thread.is_alive() and w.proxy._thread is None
        evicted = [h["body"] for h in w.sync.read_host() if h["body"].get("kind") == "evicted"]
        assert evicted == [{"kind": "evicted", "vm_state": "none"}]
        # the next request rebuilds it from disk: the settled input replays, a new one runs
        w2 = await workers.open(tenant, "s1", FakeRegistry())
        assert w2 is not w and not w2.closed
        assert w2.sync.settled["in_1"]["answer"] == "first"
        assert (await w2.act({"input_id": "in_1", "text": "hi"}))["answer"] == "first"
        assert (await w2.act({"input_id": "in_2", "text": "again"}))["answer"] == "first"
        assert w2.sync.seen_inputs == {"in_1", "in_2"}
    finally:
        await workers.shutdown(park_s=0)


@pytest.mark.asyncio
async def test_a_worker_with_a_task_a_stream_or_a_recent_touch_is_kept(tmp_path):
    store = DirStore(tmp_path / "sessions")
    workers = Workers(sandbox_factory=lambda: FakeSandbox([{"text": "x"}]), sessions=store,
                      evict_s=0, device_calls=DeviceCalls(), **worker_kwargs())
    tenant = FakeTenant()
    try:
        w = await workers.open(tenant, "s1", FakeRegistry())
        assert await workers.evict_idle(grace_s=60) == []          # touched just now
        sink = w.attach("in_1")
        assert await workers.evict_idle(grace_s=0) == []           # a stream attached
        w.detach(sink)
        assert await workers.evict_idle(grace_s=0) == [KEY]
        assert workers.get(KEY) is None and w.closed
    finally:
        await workers.shutdown(park_s=0)


def _parked_session(store: DirStore, *, device_call: bool) -> None:
    """A session whose log ends parked (checkpoint + parked) with a device
    call open (or none), as a shutdown park leaves it."""
    session = store.get_or_create("u", "t", "s1")
    sync = StateSync(session)
    events = text_task()[:4]
    events.append(ev(5, "llm_response", {"turn_id": "t1", "finish_reason": "tool_calls",
                                         "usage": {}, "message": {"role": "assistant",
                                                                  "content": None,
                                                                  "tool_calls": []}}))
    if device_call:
        events.append(ev(6, "tool_call", {"turn_id": "t1", "call_id": "c_dev",
                                          "name": "get_calendar_events", "side": "device",
                                          "args": {"day": "mon"}, "state": "RUNNING"}))
    events.append(ev(len(events) + 1, "checkpoint", checkpoint_body(events)))
    events.append(ev(len(events) + 1, "parked", {"checkpoint_seq": len(events),
                                                 "reason": "shutdown"}))
    for e in events:
        sync.accept(e)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("what", ["device_call", "question", "plain"])
async def test_a_parked_session_with_a_device_call_or_a_question_is_not_evicted(tmp_path, what):
    store = DirStore(tmp_path / "sessions")
    session = _parked_session(store, device_call=what == "device_call")
    info = {"cseq": 1, "reason": "shutdown", "answer_wait_s": 60}
    if what == "question":
        # a workflow-path question park: its timer is Temporal's, the park is held here
        info.update(question={"input_id": "in_1", "question_id": "q1"}, driver="workflow",
                    answer_deadline_ms=None)
    (session.root / "vm").mkdir(exist_ok=True)
    (session.root / "vm" / "parked.json").write_text(json.dumps(info))
    dc = DeviceCalls()
    workers = Workers(sandbox_factory=lambda: FakeSandbox([]), sessions=store, evict_s=0,
                      device_calls=dc, **worker_kwargs())
    try:
        [w] = await workers.load_parked(FakeTenant(), FakeRegistry())
        assert w.vm_state == "parked"
        if what == "device_call":
            assert dc.open_for(w.ref) == [{"id": "c_dev", "name": "get_calendar_events",
                                           "arguments": {"day": "mon"}}]   # args from the fold
            assert "c_dev" in w.adopted
        if what == "question":
            assert w.question_park == {"input_id": "in_1", "question_id": "q1"}
        evicted = await workers.evict_idle(grace_s=0)
        if what == "plain":
            assert evicted == [KEY] and w.closed
            # rebuilt on demand, still parked (the snapshot is the sandbox's)
            again = await workers.open(FakeTenant(), "s1", FakeRegistry())
            assert again is not w and again.vm_state == "parked"
        else:
            assert evicted == [] and workers.get(KEY) is w and not w.closed
    finally:
        await workers.shutdown(park_s=0)


@pytest.mark.asyncio
async def test_a_raising_on_closed_hook_breaks_no_eviction_or_close(tmp_path):
    """Issue #56, review request 3: an `on_closed` hook that raises is logged; every evicted
    Worker is still closed, and so is one closed by key."""
    store = DirStore(tmp_path / "sessions")
    workers = Workers(sandbox_factory=lambda: FakeSandbox([]), sessions=store, evict_s=0,
                      device_calls=DeviceCalls(), **worker_kwargs())

    def boom(key):
        raise ValueError(key)
    workers.on_closed = boom
    tenant = FakeTenant()
    try:
        evicted = [await workers.open(tenant, sid, FakeRegistry()) for sid in ("s1", "s2")]
        assert len(await workers.evict_idle(grace_s=0)) == 2
        assert all(w.closed for w in evicted)
        w = await workers.open(tenant, "s3", FakeRegistry())
        await workers.close(session_key("u", "t", "s3"))
        assert w.closed and workers.get(session_key("u", "t", "s3")) is None
    finally:
        await workers.shutdown(park_s=0)
