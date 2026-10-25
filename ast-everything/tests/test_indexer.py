"""Add-only indexing of the fixture repo into the fake store (plan step 3)."""
from __future__ import annotations

from collections import Counter

import mongomock
import pytest

from conftest import NODE_FIELDS
from ast_everything import indexer
from ast_everything.indexer import index_files, main, sync
from ast_everything.store import Store

UTILS = "pkg/utils.py"
SHAPES = "pkg/shapes.py"
COLLECTIONS = ("files", "nodes", "edges", "chunks")


def run(store, repo):
    return sync(store, "code", repo)


def counts(store) -> dict[str, int]:
    return {name: store.db[name].count_documents({}) for name in COLLECTIONS}


def latest(store, nid):
    return store.nodes.find_one({"nid": nid, "is_latest": True})


def edit(repo, rel, old, new):
    path = repo / rel
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_sync_writes_shared_schema(store, repo):
    """Criterion: the code index lives in the store, every node in the shared schema with a `code/` ID,
    one chunk per class or function with the ID on its first line, and the counts say what happened."""
    result = run(store, repo)
    assert dict(result) == {"files": 12, "unchanged": 0, "skipped": 2, "nodes_inserted": 35, "nodes_removed": 0}
    nodes = list(store.nodes.find())
    assert len(nodes) == 35 and all(NODE_FIELDS <= set(doc) for doc in nodes)
    assert all(doc["nid"] == "code" or doc["nid"].startswith("code/") for doc in nodes)
    assert all(doc["version"] == 1 and doc["is_latest"] for doc in nodes)
    chunks = list(store.chunks.find())
    assert len(chunks) == 20 and all(c["_id"] == f"{c['nid']}@v1#1" for c in chunks)
    assert store.chunks.find_one({"nid": f"code/{SHAPES}:Shape"})["text"] == f"code/{SHAPES}:Shape\nclass Shape:\n    pass"
    assert store.files.count_documents({"source": "code", "hash": {"$ne": None}}) == 10
    row = store.files.find_one({"path": UTILS})
    assert row["size"] == (repo / UTILS).stat().st_size and len(row["versions"]) == 1


def test_unchanged_sync_inserts_nothing(store, repo):
    """Criterion: running again on an unchanged folder inserts nothing."""
    run(store, repo)
    before = counts(store)
    result = run(store, repo)
    assert dict(result) == {"files": 12, "unchanged": 10, "skipped": 2, "nodes_inserted": 0, "nodes_removed": 0}
    assert counts(store) == before


def test_change_makes_new_version(store, repo):
    """Criterion: a changed function gets `@v2` with a `supersedes` edge; `@v1` is kept, superseded."""
    run(store, repo)
    edit(repo, UTILS, "    return a + b\n", "    return b + a\n")
    run(store, repo)
    nid = f"code/{UTILS}:calculate_sum"
    v1, v2 = store.nodes.find_one({"_id": f"{nid}@v1"}), latest(store, nid)
    assert v2["_id"] == f"{nid}@v2" and v2["text"].endswith("return b + a")
    assert (v1["is_latest"], v1["superseded_by"]) == (False, v2["_id"]) and v1["superseded_at"] is not None
    assert store.edges.find_one({"type": "supersedes", "src": v2["_id"]})["dst"] == v1["_id"]
    assert [c["is_latest"] for c in store.chunks.find({"nid": nid}, sort=[("_id", 1)])] == [False, True]


def test_one_edit_one_version(store, repo):
    """Criterion: editing one function writes one node version, one chunk and one `supersedes`
    edge, and nothing else; the other nodes keep their version and follow the file's lines."""
    run(store, repo)
    before = counts(store)
    edit(repo, UTILS, "    return a + b\n", "    total = a + b\n    return total\n")
    result = run(store, repo)
    assert (result["nodes_inserted"], result["unchanged"]) == (1, 9)
    after = counts(store)
    assert {k: after[k] - before[k] for k in COLLECTIONS} == {"files": 0, "nodes": 1, "edges": 1, "chunks": 1}
    moved = latest(store, f"code/{UTILS}:MathUtils")
    assert moved["version"] == 1 and moved["start_line"] == 9
    assert len(store.files.find_one({"path": UTILS})["versions"]) == 2


def test_edges_replaced_by_src_path(store, repo):
    """Criterion: a changed file's edges are updated by `src_path`; an edge that stays is the
    same document, one that is gone is deactivated, and nothing is deleted."""
    run(store, repo)
    kept = store.edges.find_one({"src_path": "pkg/sub/helpers.py", "type": "imports", "dst": "code/pkg/sub/tools.py"})
    edit(repo, "pkg/sub/helpers.py", "from ..utils import MathUtils\n", "")
    edit(repo, "pkg/sub/helpers.py", "class Fancy(MathUtils):", "class Fancy:")
    before = counts(store)
    run(store, repo)
    assert counts(store)["edges"] == before["edges"] + 2   # Fancy and helpers.py changed: two supersedes edges
    assert store.edges.find_one({"_id": kept["_id"]})["active"]
    gone = list(store.edges.find({"src_path": "pkg/sub/helpers.py", "active": False}))
    assert {(e["type"], e["dst"]) for e in gone} == {
        ("imports", "code/pkg/utils.py:MathUtils"), ("inherits", "code/pkg/utils.py:MathUtils")}
    assert all(e["deactivated_at"] is not None for e in gone)


def test_import_resolves_through_store(store, repo):
    """Criterion: a cross-file import resolves by an indexed `nid` lookup in the store, not by
    loading every node: `main.py` is indexed in a later call than `pkg/utils.py`."""
    index_files(store, "code", repo, [repo / UTILS])
    assert store.edges.count_documents({"src_path": "main.py"}) == 0
    index_files(store, "code", repo, [repo / "main.py"])
    imports = {e["dst"] for e in store.edges.find({"src_path": "main.py", "type": "imports"})}
    assert imports == {f"code/{UTILS}", f"code/{UTILS}:calculate_sum"}   # pkg/shapes.py is not indexed yet


def test_importer_follows_a_target_that_appears_or_goes(store, repo, monkeypatch):
    """Criterion: a cross-file edge is refreshed when only its target changes: `main.py` is not
    re-indexed, yet it loses the edges to `calculate_sum` when the function goes and gets them back
    when it returns; a file whose entities only changed inside re-resolves nothing."""
    run(store, repo)
    target = f"code/{UTILS}:calculate_sum"
    into_target = {("imports", "code/main.py"), ("invokes", "code/main.py:bare_imported")}

    def active_from_main():
        return {(e["type"], e["src"], e["dst"]) for e in store.edges.find({"src_path": "main.py", "active": True})}
    assert {(kind, src, target) for kind, src in into_target} <= active_from_main()
    text = (repo / UTILS).read_text()
    edit(repo, UTILS, "def calculate_sum(a, b):\n    return a + b\n", "")
    result = run(store, repo)
    assert (result["unchanged"], result["nodes_removed"]) == (9, 1)
    edges = active_from_main()
    assert not {e for e in edges if e[2] == target} and ("imports", "code/main.py", f"code/{UTILS}") in edges
    (repo / UTILS).write_text(text)
    run(store, repo)
    assert {(kind, src, target) for kind, src in into_target} <= active_from_main()
    assert store.edges.count_documents({"src_path": "main.py", "active": False}) == 2
    reshaped = []
    monkeypatch.setattr(indexer, "_resolve_importers", lambda store, source, root, nids, indexed, types, prefix: reshaped.append(nids))
    edit(repo, UTILS, "    return a + b\n", "    return b + a\n")
    run(store, repo)
    assert reshaped == [set()]


def test_importer_changed_in_the_same_sync_sees_the_removal(store, repo):
    """Criterion: edges are resolved once every node of the sync is written: `main.py`, changed in
    the sync that removes `calculate_sum` from a file indexed after it, gets no edge to the removed node."""
    run(store, repo)
    edit(repo, UTILS, "def calculate_sum(a, b):\n    return a + b\n", "")
    edit(repo, "main.py", "    return LIMIT\n", "    return LIMIT + 1\n")
    run(store, repo)
    assert store.edges.find_one({"dst": f"code/{UTILS}:calculate_sum", "active": True}) is None


def test_removed_file_marked(store, repo):
    """Criterion: a removed file and its nodes get `removed_at`, its edges go inactive, nothing is deleted."""
    run(store, repo)
    before = counts(store)
    (repo / SHAPES).unlink()
    result = run(store, repo)
    assert result["nodes_removed"] == 2 and counts(store) == before
    shape = store.nodes.find_one({"nid": f"code/{SHAPES}:Shape"})
    assert shape["is_latest"] is False and shape["removed_at"] is not None
    assert store.chunks.find_one({"nid": shape["nid"]})["is_latest"] is False
    assert store.files.find_one({"path": SHAPES})["removed_at"] is not None
    assert not any(e["active"] for e in store.edges.find({"$or": [{"src_path": SHAPES}, {"dst": shape["nid"]}]}))


def test_restore_continues_versions(store, repo):
    """Criterion: a deleted then restored file is indexed again; an unchanged node comes back as
    latest and a changed one continues its version numbers."""
    run(store, repo)
    text = (repo / SHAPES).read_text()
    (repo / SHAPES).unlink()
    run(store, repo)
    (repo / SHAPES).write_text(text)
    run(store, repo)
    shape = latest(store, f"code/{SHAPES}:Shape")
    assert shape["version"] == 1 and shape["removed_at"] is None
    assert store.chunks.find_one({"nid": shape["nid"]})["is_latest"] is True
    assert {e["src"] for e in store.edges.find({"dst": shape["nid"], "active": True})} == {
        f"code/{SHAPES}", "code/main.py:module_attr", "code/main.py:Square"}
    assert store.files.find_one({"path": SHAPES})["removed_at"] is None
    (repo / SHAPES).unlink()
    run(store, repo)
    (repo / SHAPES).write_text(text.replace("pass", "sides = 0"))
    run(store, repo)
    shape = latest(store, f"code/{SHAPES}:Shape")
    assert shape["version"] == 2 and shape["text"].endswith("sides = 0")
    assert store.nodes.find_one({"_id": f"code/{SHAPES}:Shape@v1"})["superseded_by"] == shape["_id"]


def test_moved_file_is_remove_plus_add(store, repo):
    """Criterion: a moved file is a remove plus an add; its old nodes get `removed_at`, the new ones
    start at `@v1`, and the old file's edges are inactive."""
    run(store, repo)
    (repo / SHAPES).rename(repo / "pkg/forms.py")
    run(store, repo)
    old = store.nodes.find_one({"nid": f"code/{SHAPES}:Shape"})
    new = latest(store, "code/pkg/forms.py:Shape")
    assert old["removed_at"] is not None and old["is_latest"] is False
    assert new["version"] == 1
    assert not any(e["active"] for e in store.edges.find({"src_path": SHAPES}))
    assert store.edges.find_one({"src_path": "pkg/forms.py", "type": "contains", "dst": "code/pkg/forms.py"})["active"]


def test_broken_file_reported(store, repo):
    """Criterion: a file that does not parse lands in `files` with its reason, and the rest is indexed."""
    run(store, repo)
    rows = {row["path"]: row for row in store.files.find({"skipped_reason": {"$ne": None}})}
    assert rows.keys() == {"broken.py", "binary.py"}
    assert rows["broken.py"]["skipped_reason"].startswith("SyntaxError") and rows["broken.py"]["hash"] is None
    assert latest(store, "code/main.py") and not store.nodes.find_one({"path": "broken.py"})


def test_sync_command_prints_counts(store, repo, monkeypatch, capsys):
    """`python -m ast_everything.indexer sync <folder> --source code` prints the source and the counts."""
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls: store))
    main(["sync", str(repo), "--source", "code"])
    assert capsys.readouterr().out == ("source: code\nfiles: 12\nunchanged: 0\nskipped: 2\n"
                                       "nodes inserted: 35\nnodes removed: 0\n")
    assert "nid_1" in store.nodes.index_information()


def test_sync_command_names_the_source_after_the_folder(store, repo, monkeypatch, capsys):
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls: store))
    main(["sync", str(repo)])
    assert capsys.readouterr().out.startswith("source: repo\n")
    assert latest(store, "repo/main.py")


@pytest.mark.parametrize("name", ["", "a/b", "a#b", "a:b", "-lead", "x" * 101])
def test_a_source_name_that_cannot_head_an_id_is_refused(store, repo, name):
    with pytest.raises(ValueError, match="source name"):
        index_files(store, name, repo, [])


def test_source_name_from_a_folder_name():
    assert indexer.source_name("My Project (v2)") == "My-Project-v2"
    assert indexer.source_name("...") == "source"


def active_edges(store) -> set[tuple[str, str, str]]:
    return {(e["type"], e["src"], e["dst"]) for e in store.edges.find({"active": True})}


def test_interrupted_sync_resumes_with_every_edge(store, repo, monkeypatch):
    """The manifest is written after the edges: a sync that stops in the edge pass leaves its
    files unindexed, and the next sync writes them whole, inserting no node twice."""
    real = indexer._write_edges

    def fail_once(*args):
        monkeypatch.setattr(indexer, "_write_edges", real)
        raise RuntimeError("stopped")
    monkeypatch.setattr(indexer, "_write_edges", fail_once)
    with pytest.raises(RuntimeError, match="stopped"):
        run(store, repo)
    assert store.files.count_documents({"hash": {"$ne": None}}) == 0     # no file counts as indexed
    resumed = run(store, repo)
    assert (resumed["unchanged"], resumed["nodes_inserted"]) == (0, 0)  # every node was kept
    fresh = type(store)(mongomock.MongoClient(tz_aware=True), "fresh")
    run(fresh, repo)
    assert active_edges(store) == active_edges(fresh) and active_edges(store)
    assert ({r["path"]: r["hash"] for r in store.files.find()} == {r["path"]: r["hash"] for r in fresh.files.find()})


class Counting:
    """A collection that counts the calls made on it."""

    def __init__(self, collection, calls: Counter):
        self._collection, self._calls = collection, calls

    def __getattr__(self, name):
        attr = getattr(self._collection, name)
        if callable(attr):
            def call(*args, **kwargs):
                self._calls[f"{self._collection.name}.{name}"] += 1
                return attr(*args, **kwargs)
            return call
        return attr


def store_calls(store, action) -> Counter:
    calls: Counter = Counter()
    names = ("files", "nodes", "edges", "chunks")
    originals = {name: getattr(store, name) for name in names}
    for name in names:
        setattr(store, name, Counting(originals[name], calls))
    try:
        action()
    finally:
        for name in names:
            setattr(store, name, originals[name])
    return calls


def test_unchanged_sync_makes_the_same_store_calls_however_many_files(store, repo):
    """An unchanged re-sync reads the manifest once; it does not make a call per file."""
    run(store, repo)
    small = store_calls(store, lambda: run(store, repo))
    for n in range(30):
        (repo / f"extra_{n}.py").write_text(f"def f{n}():\n    return {n}\n")
    run(store, repo)
    large = store_calls(store, lambda: run(store, repo))
    assert small == large and sum(large.values()) <= 6


def test_one_edited_function_writes_one_version_one_chunk_one_supersedes(store, repo):
    run(store, repo)
    before = counts(store)
    edit(repo, UTILS, "    return a + b\n", "    return b + a\n")
    result = run(store, repo)
    after = counts(store)
    assert result["nodes_inserted"] == 1
    assert (after["nodes"] - before["nodes"], after["chunks"] - before["chunks"]) == (1, 1)
    assert store.edges.count_documents({"type": "supersedes"}) == 1
