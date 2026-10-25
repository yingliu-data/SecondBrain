"""A chat session's turns into the fake store (issue #56, plan step 1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import NODE_FIELDS
from ast_everything import memory
from ast_everything.memory import Turn, index_session
from ast_everything.store import now

SOURCE = "memory-alice"
SESSION = "sessions/default/s_42"
SID = f"{SOURCE}/{SESSION}"
TITLE = "Choosing an embedding"
AT = datetime(2026, 10, 24, 13, 41, tzinfo=timezone.utc)
COLLECTIONS = ("nodes", "edges", "chunks")


def turns(*ns: int) -> list[Turn]:
    return [Turn(n, "user" if n % 2 else "assistant", AT + timedelta(minutes=n), f"turn {n} text") for n in ns]


def run(store, *ns: int):
    return index_session(store, SOURCE, SESSION, TITLE, turns(*ns))


def counts(store) -> dict[str, int]:
    return {name: store.db[name].count_documents({}) for name in COLLECTIONS}


def edges(store, edge_type: str) -> set[tuple[str, str]]:
    return {(doc["src"], doc["dst"]) for doc in store.edges.find({"type": edge_type})}


def test_turns_become_nodes(store):
    """Criterion 3: turns become session, turn and chunk nodes with `contains` and `precedes`
    edges, and the session record holds the cursor with the lease released."""
    result = run(store, 1, 2, 3)
    assert dict(result) == {"turns": 3, "nodes_inserted": 4, "chunks_inserted": 3, "edges_inserted": 5}
    session = store.nodes.find_one({"nid": SID})
    assert NODE_FIELDS <= set(session) and session["type"] == "session" and session["names"] == ["s_42"]
    turn = store.nodes.find_one({"nid": f"{SID}:turn-2"})
    assert (turn["type"], turn["said_by"], turn["at"], turn["text"]) == ("turn", "assistant", AT + timedelta(minutes=2), "turn 2 text")
    assert store.chunks.find_one({"_id": f"{SID}:turn-2@v1#1"})["text"] == f"{TITLE}\nturn 2 text"
    ids = [f"{SID}:turn-{n}" for n in (1, 2, 3)]
    assert edges(store, "contains") == {(SID, nid) for nid in ids}
    assert edges(store, "precedes") == {(ids[0], ids[1]), (ids[1], ids[2])}
    record = store.sessions.find_one({"_id": SID})
    assert (record["source"], record["indexed_through_turn"], record["last_turn"]) == (SOURCE, 3, 3)
    assert (record["status"], record["lock_until"]) == ("idle", None) and record["indexed_at"] is not None


def test_second_run_inserts_nothing(store):
    """Criterion 3 and edge case "indexed twice": a second run of the same session inserts nothing."""
    run(store, 1, 2, 3)
    before = counts(store)
    assert dict(run(store, 1, 2, 3)) == {"turns": 0, "nodes_inserted": 0, "chunks_inserted": 0, "edges_inserted": 0}
    assert counts(store) == before


def test_resume_indexes_only_new_turns(store):
    """Criterion 3 and edge case "resumes after it was indexed": only the new turns are written,
    and the last indexed turn `precedes` the first new one."""
    run(store, 1, 2)
    result = run(store, 1, 2, 3, 4)
    assert dict(result) == {"turns": 2, "nodes_inserted": 2, "chunks_inserted": 2, "edges_inserted": 4}
    assert (f"{SID}:turn-2", f"{SID}:turn-3") in edges(store, "precedes")
    assert store.sessions.find_one({"_id": SID})["indexed_through_turn"] == 4


def test_long_turn_is_split_into_chunks(store):
    """Criterion 3: a turn over about 500 words is split at paragraphs, each part headed by the title."""
    paragraphs = [" ".join([f"p{i}"] * 200) for i in range(3)]
    index_session(store, SOURCE, SESSION, TITLE, [Turn(1, "user", AT, "\n\n".join(paragraphs))])
    chunks = sorted(store.chunks.find(), key=lambda doc: doc["_id"])
    assert [doc["_id"] for doc in chunks] == [f"{SID}:turn-1@v1#1", f"{SID}:turn-1@v1#2"]
    assert chunks[0]["text"] == f"{TITLE}\n{paragraphs[0]}\n\n{paragraphs[1]}"
    assert chunks[1]["text"] == f"{TITLE}\n{paragraphs[2]}"


def crash_before_commit(store, monkeypatch):
    """A run that writes the turns and dies before it moves the cursor, leaving its lease held."""
    def boom(*_):
        raise RuntimeError("crash before commit")
    with monkeypatch.context() as patch:
        patch.setattr(memory, "_commit", boom)
        with pytest.raises(RuntimeError):
            run(store, 1, 2, 3)


def test_crash_before_commit_then_rerun(store, monkeypatch):
    """Edge case "a run crashes": once the lease runs out, the next run starts from the old
    cursor, inserts nothing twice and completes."""
    crash_before_commit(store, monkeypatch)
    before = counts(store)
    assert store.sessions.find_one({"_id": SID})["indexed_through_turn"] == 0
    monkeypatch.setattr(memory, "now", lambda: now() + timedelta(seconds=memory.LEASE_S + 1))
    result = run(store, 1, 2, 3)
    assert dict(result) == {"turns": 3, "nodes_inserted": 0, "chunks_inserted": 0, "edges_inserted": 0}
    assert counts(store) == before
    assert store.sessions.find_one({"_id": SID})["indexed_through_turn"] == 3


def test_held_lease_skips(store, monkeypatch):
    """Edge case "the end event and the idle check fire together": while one run holds the
    lease, another skips the session and writes nothing."""
    crash_before_commit(store, monkeypatch)
    before = counts(store)
    assert run(store, 1, 2, 3, 4) is None
    assert counts(store) == before
    assert store.sessions.find_one({"_id": SID})["status"] == "indexing"


def write_memory(root, slug: str, body: str) -> None:
    root.mkdir(exist_ok=True)
    (root / f"{slug}.md").write_text(f"---\nname: {slug}\ndescription: about {slug}\ntype: user\n---\n\n{body}\n")


def run_with_memory(store, root, files: dict[str, str]):
    return index_session(store, SOURCE, SESSION, TITLE, turns(1), memory_root=root, memory_files=files)


def test_dirty_files_only(store, tmp_path):
    """Criterion 4: a memory file the session wrote gets the doc converter's next version and a
    `supersedes` edge; a file it did not touch keeps its manifest row, even when it changed on disk."""
    root = tmp_path / "memory"
    write_memory(root, "editor", "Uses vim.")
    write_memory(root, "language", "Writes Python.")
    run_with_memory(store, root, {"editor": "write", "language": "write"})
    untouched = store.files.find_one({"path": "language.md"})
    write_memory(root, "editor", "Uses helix now.")
    write_memory(root, "language", "Writes Rust.")
    result = run_with_memory(store, root, {"editor": "write"})
    assert result["files"] == 1
    doc = store.nodes.find_one({"nid": f"{SOURCE}/editor.md", "is_latest": True})
    assert doc["_id"] == f"{SOURCE}/editor.md@v2"
    assert store.edges.find_one({"type": "supersedes", "src": doc["_id"], "dst": f"{SOURCE}/editor.md@v1"})
    assert store.files.find_one({"path": "language.md"}) == untouched


def test_forgotten_file_is_marked_removed(store, tmp_path):
    """Criterion 4: a file the session forgot gets `removed_at` on its nodes and its manifest row,
    and nothing is deleted."""
    root = tmp_path / "memory"
    write_memory(root, "editor", "Uses vim.")
    run_with_memory(store, root, {"editor": "write"})
    before = counts(store)
    (root / "editor.md").unlink()
    result = run_with_memory(store, root, {"editor": "remove"})
    assert result["nodes_removed"] > 0 and counts(store) == before
    assert all(doc["removed_at"] and not doc["is_latest"] for doc in store.nodes.find({"path": "editor.md"}))
    assert store.files.find_one({"path": "editor.md"})["removed_at"] is not None
