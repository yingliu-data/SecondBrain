"""The memory indexer's three triggers, on mongomock, with event-log file times set back
for the idle check (issue #56, plan step 4)."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.host import memory_index
from app.host.memory_index import IDLE_S, MemoryIndexer
from app.host.sides import DeviceCalls
from app.host.worker import Workers
from app.session.dir_store import DirStore
from app.session.ids import session_key
from eventlog_fakes import HASH, LogBuilder, store
from host_fakes import FakeRegistry, FakeSandbox, FakeTenant, users_root, worker_kwargs  # noqa: F401
from test_ast_index_skill import mongo  # noqa: F401

KEY = session_key("u", "t", "s1")
SESSION = "memory-u/sessions/t/s1"


@pytest.fixture
def indexer(mongo, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_index, "USERS_ROOT", str(tmp_path / "users"))
    return MemoryIndexer(DirStore(tmp_path / "sessions"), [FakeTenant(), FakeTenant(name="guest")],
                         connect=lambda: mongo)


@pytest.fixture
def runs(monkeypatch) -> list[str]:
    """The session of each index run."""
    calls, real = [], memory_index.index_session

    def counted(*args, **kwargs):
        calls.append(args[2])
        return real(*args, **kwargs)
    monkeypatch.setattr(memory_index, "index_session", counted)
    return calls


def chat(indexer, tmp_path, tenant: str = "t", session_id: str = "s1"):
    """A session in which the user asked the agent to remember their editor."""
    session = indexer.sessions.get_or_create("u", tenant, session_id)
    log = LogBuilder(session_id=session_id, tenant=tenant).start().tool_task("in_1", "remember my editor", "noted")
    log.emit("memory", {"scope": "user", "op": "write", "slug": "editor", "sha256": HASH, "derived_from": "c1"})
    store(session, log.events)
    memory = tmp_path / "users" / "u" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "editor.md").write_text("---\nname: editor\ndescription: the editor\ntype: user\n---\n\nUses vim.\n")
    return session


def age(session, seconds: float) -> None:
    then = time.time() - seconds
    for path in session.root.glob("events/r*/*.jsonl"):
        os.utime(path, (then, then))


def turns(mongo) -> int:
    return mongo.nodes.count_documents({"type": "turn"})


@pytest.mark.asyncio
async def test_nothing_indexed_while_open(indexer, mongo, runs, tmp_path):
    """Criterion 1: nothing is indexed while the session's Worker is open; closing it indexes the session."""
    workers = Workers(sandbox_factory=lambda: FakeSandbox([{"text": "first"}]), sessions=indexer.sessions,
                      evict_s=0, device_calls=DeviceCalls(), **worker_kwargs())
    closing = []
    workers.on_closed = lambda key: closing.append(indexer.closed(key))
    try:
        w = await workers.open(FakeTenant(), "s1", FakeRegistry())
        assert (await w.act({"input_id": "in_1", "text": "hi"}))["answer"] == "first"
        await indexer.check(IDLE_S)
        assert runs == [] and turns(mongo) == 0
        await workers.close(KEY)
        await asyncio.gather(*closing)
    finally:
        await workers.shutdown(park_s=0)
    assert runs == ["sessions/t/s1"] and turns(mongo) == 2


@pytest.mark.asyncio
async def test_close_indexes_once(indexer, mongo, runs, tmp_path):
    """Criterion 2: a close is one run; it indexes the turns and the memory file the session wrote."""
    chat(indexer, tmp_path)
    await indexer.closed(KEY)
    assert runs == ["sessions/t/s1"] and turns(mongo) == 2
    assert mongo.nodes.find_one({"nid": SESSION})
    assert mongo.nodes.find_one({"nid": "memory-u/editor.md", "is_latest": True})


@pytest.mark.asyncio
async def test_idle_30_minutes_indexes_once(indexer, mongo, runs, tmp_path):
    """Criterion 2: a session idle 31 minutes is indexed by the check, and not again by the next one."""
    age(chat(indexer, tmp_path), 31 * 60)
    await indexer.check(IDLE_S)
    await indexer.check(IDLE_S)
    assert runs == ["sessions/t/s1"] and turns(mongo) == 2


@pytest.mark.asyncio
async def test_active_session_not_indexed(indexer, runs, tmp_path):
    """Criterion 2: a session last active 10 minutes ago is not indexed by the idle check."""
    age(chat(indexer, tmp_path), 10 * 60)
    await indexer.check(IDLE_S)
    assert runs == []


@pytest.mark.asyncio
async def test_startup_catch_up(indexer, mongo, runs, tmp_path):
    """Criterion 2: at startup, `run` indexes a session not indexed yet, however recent its log."""
    chat(indexer, tmp_path)
    task = asyncio.create_task(indexer.run())
    try:
        while not mongo.sessions.find_one({"_id": SESSION, "indexed_at": {"$ne": None}}):
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert runs == ["sessions/t/s1"]


@pytest.mark.asyncio
async def test_close_and_idle_together_run_once(indexer, runs, tmp_path):
    """Edge case "the end event and the idle check fire together": only one run proceeds."""
    age(chat(indexer, tmp_path), 31 * 60)
    await asyncio.gather(indexer.closed(KEY), indexer.check(IDLE_S))
    assert runs == ["sessions/t/s1"]


@pytest.mark.asyncio
async def test_store_down_retries_later(indexer, mongo, runs, tmp_path, caplog):
    """Edge case "Atlas is down when the session ends": a warning, no error to the caller,
    the session stays unindexed, and the next idle check indexes it."""
    def down():
        raise RuntimeError("no server")
    indexer.connect = down
    session = chat(indexer, tmp_path)
    await indexer.closed(KEY)
    assert "memory index of session s1 failed: RuntimeError" in caplog.text and turns(mongo) == 0
    indexer.connect = lambda: mongo
    age(session, 31 * 60)
    await indexer.check(IDLE_S)
    assert turns(mongo) == 2


@pytest.mark.asyncio
async def test_guest_tenant_skipped(indexer, runs, tmp_path):
    """Plan part 9, question 5: the guest tenant's sessions are not indexed, at close or by the check."""
    chat(indexer, tmp_path, tenant="guest")
    assert indexer.closed(session_key("u", "guest", "s1")) is None
    await indexer.check(0)
    assert runs == []


@pytest.mark.asyncio
async def test_turns_written_during_a_run_are_indexed_next(indexer, runs, tmp_path, monkeypatch):
    """Criterion 2 (review request 4): a log write that lands after a run read the turns, but
    before it committed, makes the next trigger index the session again."""
    session = chat(indexer, tmp_path)
    age(session, 60)
    read = memory_index.session_turns

    def read_then_write(root):
        turns = read(root)
        age(session, 0)
        return turns
    monkeypatch.setattr(memory_index, "session_turns", read_then_write)
    await indexer.closed(KEY)
    monkeypatch.setattr(memory_index, "session_turns", read)
    await indexer.check(0)
    assert runs == ["sessions/t/s1", "sessions/t/s1"]


@pytest.mark.asyncio
async def test_a_backlog_is_indexed_a_few_sessions_per_check(indexer, runs, tmp_path, monkeypatch, caplog):
    """Issue #56, review round 2, request 2: the first start indexes at most `MAX_PER_CHECK` sessions,
    logs the size of the backlog, and the next check goes on with the rest."""
    monkeypatch.setattr(memory_index, "MAX_PER_CHECK", 1)
    chat(indexer, tmp_path, session_id="s1")
    chat(indexer, tmp_path, session_id="s2")
    await indexer.check(0)
    assert len(runs) == 1 and "memory index: 2 sessions due, 1 indexed now" in caplog.text
    await indexer.check(0)
    assert sorted(runs) == ["sessions/t/s1", "sessions/t/s2"]


@pytest.mark.asyncio
async def test_a_failing_session_gives_its_slot_back(indexer, runs, tmp_path, monkeypatch):
    """Review round 3, request 1: a session whose run failed comes after the other due sessions,
    so a session that always fails does not keep the one slot of each check."""
    monkeypatch.setattr(memory_index, "MAX_PER_CHECK", 1)
    real = memory_index.index_session

    def fails_on_s1(store, source, session, *args, **kwargs):
        if session == "sessions/t/s1":
            raise RuntimeError("bad session")
        return real(store, source, session, *args, **kwargs)
    monkeypatch.setattr(memory_index, "index_session", fails_on_s1)
    chat(indexer, tmp_path, session_id="s1")
    chat(indexer, tmp_path, session_id="s2")
    monkeypatch.setattr(indexer, "_due", lambda user, tenant, idle_s: ["s1", "s2"])
    await indexer.index("u", "t", "s1")
    await indexer.check(0)
    assert runs == ["sessions/t/s2"]


@pytest.mark.asyncio
async def test_the_first_run_creates_the_indexes(indexer, mongo, tmp_path):
    """Review round 4, request 3: the indexer creates the store's indexes before it writes."""
    chat(indexer, tmp_path)
    await indexer.closed(KEY)
    assert "nid_1" in mongo.nodes.index_information() and "src_1" in mongo.edges.index_information()
