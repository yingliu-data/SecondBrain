"""The memory indexer's three triggers, on mongomock, with event-log file times set back
for the idle check (issue #56, plan step 4)."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest

from app.host import memory_index
from app.host import worker as worker_module
from app.host.memory_index import IDLE_S, MemoryIndexer
from app.host.sides import DeviceCalls
from app.host.worker import Worker, Workers
from app.session.dir_store import DirStore
from app.session.ids import session_key
from app.skills import base
from app.skills.remember import skill as remember_module
from app.skills.remember.skill import RememberSkill
from eventlog_fakes import HASH, LogBuilder, store
from host_fakes import FakeRegistry, FakeSandbox, FakeTenant, users_root, worker_kwargs  # noqa: F401
from test_ast_index_skill import MockStore, mongo  # noqa: F401

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


# ---------------------------------------------------------------- memory files and recall


def write_memory(root: Path, slug: str, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{slug}.md").write_text(f"---\nname: {slug}\ndescription: about {slug}\ntype: user\n---\n\n{body}\n")


def latest(mongo, nid: str):
    return mongo.nodes.find_one({"nid": nid, "is_latest": True})


@pytest.fixture
def active(indexer):
    memory_index.activate(indexer)
    yield indexer
    memory_index.activate(None)


async def settled(indexer) -> None:
    """Wait for the indexer's background tasks."""
    while indexer._tasks:
        await asyncio.gather(*indexer._tasks)


@pytest.mark.asyncio
async def test_session_memory_is_indexed_with_the_session(indexer, mongo, tmp_path):
    """The session's own memory files (a compaction summary, `remember(scope=session)`) are indexed
    under the session's path when the session is, beside the user's files."""
    session = indexer.sessions.get_or_create("u", "t", "s1")
    log = LogBuilder().start().tool_task("in_1", "keep a note", "noted")
    log.emit("memory", {"scope": "session", "op": "write", "slug": "compaction-9", "sha256": HASH, "derived_from": "c1"})
    store(session, log.events)
    write_memory(session.memory, "compaction-9", "The user is choosing an editor.")
    await indexer.closed(KEY)
    assert latest(mongo, f"{SESSION}/memory/compaction-9.md")


@pytest.mark.asyncio
async def test_remember_and_forget_index_the_file_at_once(active, mongo, tmp_path, monkeypatch):
    """1: a file `remember` writes is in the graph without waiting for the session to close; one
    `forget` removes is marked removed; a session-scope one goes under the session."""
    monkeypatch.setattr(remember_module, "USERS_ROOT", str(tmp_path / "users"))
    session = active.sessions.get_or_create("u", "t", "s1")
    base.set_current_context(session, "u", "t", "s1")
    skill = RememberSkill()
    note = {"name": "editor", "description": "the editor", "type": "user", "body": "Uses vim."}
    await skill.execute("remember", {"slug": "editor", **note})
    await skill.execute("remember", {"slug": "scratch", "scope": "session", **note})
    await settled(active)
    assert latest(mongo, "memory-u/editor.md") and latest(mongo, f"{SESSION}/memory/scratch.md")
    await skill.execute("forget", {"slug": "editor"})
    await settled(active)
    assert mongo.files.find_one({"path": "editor.md"})["removed_at"] is not None


def test_nothing_is_indexed_without_the_indexer_or_for_the_guest(active, tmp_path):
    assert memory_index.memory_written("u", "guest", "s1", None, "user", "editor") is None
    assert memory_index.memory_written("u", "t", "s1", None, "user", "../x") is None
    memory_index.activate(None)
    assert memory_index.memory_written("u", "t", "s1", None, "user", "editor") is None
    assert memory_index.recall("u", "t", "s1", "editor") == []


@pytest.mark.asyncio
async def test_the_check_makes_the_folder_the_index(indexer, mongo, tmp_path):
    """3: a file no session's events name (written by hand) is indexed by the check, one deleted is
    marked removed, MEMORY.md is not a memory, and a folder that is gone changes nothing."""
    root = tmp_path / "users" / "u" / "memory"
    write_memory(root, "hand", "Written by hand.")
    write_memory(root, "old", "To be deleted.")
    (root / "MEMORY.md").write_text("- [hand](hand.md)\n")
    await indexer.check(IDLE_S)
    assert latest(mongo, "memory-u/hand.md") and latest(mongo, "memory-u/old.md")
    assert not mongo.files.find_one({"path": "MEMORY.md"})
    (root / "old.md").unlink()
    await indexer.check(IDLE_S)
    assert mongo.files.find_one({"path": "old.md"})["removed_at"] is not None
    shutil.rmtree(root)
    await indexer.check(IDLE_S)
    assert mongo.files.find_one({"path": "hand.md"})["removed_at"] is None


class RecallStore(MockStore):
    """Vector search as a substring match with a fixed score (`delay_s` before it answers)."""

    score = 0.9
    delay_s = 0.0
    error: Exception | None = None

    def vector(self, query, match, limit=10):
        time.sleep(self.delay_s)
        if self.error:
            raise self.error
        return [{**doc, "score": self.score} for doc in self.chunks.find(match)
                if query.lower() in doc["text"].lower()][:limit]


@pytest.fixture
def recalling(tmp_path, monkeypatch):
    """An active indexer on a store whose vector search answers, with session s1 indexed."""
    monkeypatch.setattr(memory_index, "USERS_ROOT", str(tmp_path / "users"))
    mongo = RecallStore(mongomock.MongoClient(tz_aware=True), "test")
    indexer = MemoryIndexer(DirStore(tmp_path / "sessions"), [FakeTenant()], connect=lambda: mongo)
    memory_index.activate(indexer)
    yield indexer, mongo
    memory_index.activate(None)


@pytest.mark.asyncio
async def test_recall_brings_back_another_sessions_turns(recalling, tmp_path, monkeypatch):
    """4: an input recalls the nodes of the user's graph nearest it: another session's turn, not the
    current session's, not a memory file the description recall brought, not one scoring too low."""
    indexer, mongo = recalling
    chat(indexer, tmp_path)
    await indexer.closed(KEY)
    found = memory_index.recall("u", "t", "s2", "editor", ["editor"])
    assert [(item["body"], item["source"]) for item in found] == [("remember my editor", "index")]
    assert found[0]["title"] == "earlier conversation: s1 (user, 2026-10-07)" and found[0]["score"] == 0.9
    assert {item["id"] for item in memory_index.recall("u", "t", "s2", "editor")} == {
        f"{SESSION}:turn-1", "memory-u/editor.md"}
    assert memory_index.recall("u", "t", "s1", "editor", ["editor"]) == []
    assert memory_index.recall("u", "guest", "s2", "editor") == []
    monkeypatch.setattr(memory_index, "MEMORY_RECALL_MIN_SCORE", 0.95)
    assert memory_index.recall("u", "t", "s2", "editor", ["editor"]) == []


@pytest.mark.asyncio
async def test_recall_never_holds_up_or_fails_an_input(recalling, tmp_path, monkeypatch, caplog):
    indexer, mongo = recalling
    chat(indexer, tmp_path)
    await indexer.closed(KEY)
    monkeypatch.setattr(memory_index, "RECALL_TIMEOUT_S", 0.05)
    mongo.delay_s = 0.5
    started = time.monotonic()
    assert memory_index.recall("u", "t", "s2", "editor") == []
    assert time.monotonic() - started < 0.4 and "memory recall took over" in caplog.text
    mongo.delay_s, mongo.error = 0.0, RuntimeError("no server")
    assert memory_index.recall("u", "t", "s2", "editor") == []
    assert "memory recall failed: RuntimeError: no server" in caplog.text


def test_worker_attachments_add_the_graph_after_the_description_recall(monkeypatch):
    """The worker's attachments: the description recall's, then the graph's, which skips the files
    the first brought."""
    monkeypatch.setattr(worker_module.hello_mod, "attachments",
                        lambda tenant, session, user, text: [{"kind": "memory", "slug": "editor", "body": "Uses vim."}])
    asked = []

    def recall(user, tenant, session_id, text, skip_slugs):
        asked.append((user, tenant, session_id, text, skip_slugs))
        return [{"kind": "memory", "source": "index", "title": "t", "body": "b"}]
    monkeypatch.setattr(memory_index, "recall", recall)
    worker = SimpleNamespace(tenant=SimpleNamespace(name="t"), session=None, user="u", session_id="s2")
    assert [item.get("slug") or item["title"] for item in Worker.attachments(worker, "editor")] == ["editor", "t"]
    assert asked == [("u", "t", "s2", "editor", ["editor"])]
