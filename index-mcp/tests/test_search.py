"""The four search layers, the graph walk, retrieve and by-time on the fake store (plan step 4)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pytest

from index_mcp.indexer import sync
from index_mcp.search import by_time, retrieve, search, traverse
from index_mcp.store import search_index_specs

UTILS = "pkg/utils.py"
SUM = f"code/{UTILS}:calculate_sum"
DOUBLE = f"code/{UTILS}:MathUtils.double"


@pytest.fixture
def indexed(store, repo):
    sync(store, "code", repo)
    return store


def ids(result) -> list[str]:
    return [r["id"] for r in (result["results"] if isinstance(result, dict) else result)]


def a_moment_later() -> str:
    """Now, as ISO 8601, strictly after what was stored so far: timestamps are kept at millisecond precision."""
    time.sleep(0.01)
    return datetime.now(timezone.utc).isoformat()


def edit_sum(repo):
    path = repo / UTILS
    path.write_text(path.read_text().replace("    return a + b\n", "    return b + a\n"))


def test_layer1_exact_id(indexed):
    """Criterion: an exact logical or version ID is found by layer 1, latest version only by default."""
    found = search(indexed, DOUBLE)
    assert found["layer"] == 1 and ids(found) == [f"{DOUBLE}@v1"]
    result = found["results"][0]
    assert (result["type"], result["status"], result["location"]) == ("function", "latest", f"{UTILS}:12-13")
    assert (result["heading_path"], result["preview"]) == (["MathUtils", "double"], "    def double(self, a):")
    assert search(indexed, f"{DOUBLE}@v1", detail="fold")["results"] == [
        {"id": f"{DOUBLE}@v1", "type": "function", "location": f"{UTILS}:12-13", "status": "latest",
         "heading_path": ["MathUtils", "double"]}]


def test_layer2_name(indexed):
    """Criterion: a bare name goes to layer 2, which returns every latest entity with that name,
    whatever the case of the query; the first layer with hits answers, so layers 3 and 4 do not run."""
    found = search(indexed, " Calculate_Sum ", detail="fold")
    assert found["layer"] == 2
    assert set(ids(found)) == {f"{SUM}@v1", f"code/{UTILS}:MathUtils.calculate_sum@v1",
                               "code/pkg/sub/tools.py:calculate_sum@v1"}
    assert search(indexed, "mathutils.double")["layer"] == 2
    assert search(indexed, "utils.py", node_types=["file"])["layer"] == 2


def test_layer3_keyword_over_names_not_folders(indexed):
    """Criterion: layer 3 matches names, heading path and the file stem, not a folder name."""
    found = search(indexed, "calculate", detail="fold")
    assert found["layer"] == 3 and SUM + "@v1" in ids(found)
    assert search(indexed, "utils", node_types=["file"], detail="fold")["layer"] == 3
    assert indexed.keyword_names("pkg", {"is_latest": True}) == []
    assert search(indexed, "pkg")["layer"] != 3
    nodes_index = next(spec for spec in search_index_specs() if spec["name"] == "nodes_search")
    assert set(nodes_index["definition"]["mappings"]["fields"]) == {"names", "heading_path", "stem", "is_latest", "source"}


def test_layer4_content(indexed):
    """Criterion: content search over chunks, keyword and vector fused, mapped to the node versions."""
    found = search(indexed, "a + b", detail="full")
    assert found["layer"] == 4 and ids(found) == [f"{SUM}@v1"]
    assert found["results"][0]["text"] == "def calculate_sum(a, b):\n    return a + b"
    assert search(indexed, "a + b", path_prefix="pkg/sub")["results"] == []
    assert search(indexed, "a + b", node_types=["class"])["results"] == []
    # The first chunk for this query is a function's; the node type filter still finds the class.
    found = search(indexed, "calculate_sum(a, b)", node_types=["class"], limit=1, detail="fold")
    assert (found["layer"], ids(found)) == (4, [f"code/{UTILS}:MathUtils@v1"])


def test_vector_failure_falls_back(indexed, caplog):
    """Criterion: when vector search fails, layer 4 answers by keyword alone and logs a warning."""
    indexed.vector_error = RuntimeError("no vector index")
    with caplog.at_level(logging.WARNING, logger="index_mcp.search"):
        found = search(indexed, "a + b", detail="fold")
    assert found["layer"] == 4 and ids(found) == [f"{SUM}@v1"]
    assert "vector search failed" in caplog.text and "no vector index" in caplog.text


def test_no_layer_matches(indexed):
    assert search(indexed, "zzz qqq") == {"layer": None, "results": []}


def test_history_shows_superseded(indexed, repo):
    """Criterion: by default only latest versions are found; `history` also returns superseded ones."""
    edit_sum(repo)
    sync(indexed, "code", repo)
    assert ids(search(indexed, SUM)) == [f"{SUM}@v2"]
    found = search(indexed, SUM, history=True)
    assert ids(found) == [f"{SUM}@v2", f"{SUM}@v1"]
    assert [r["status"] for r in found["results"]] == ["latest", "superseded"]
    assert search(indexed, "a + b", history=True, detail="fold")["results"][0]["id"] == f"{SUM}@v1"


def test_time_range_filters_every_layer(indexed, repo):
    before = a_moment_later()
    edit_sum(repo)
    sync(indexed, "code", repo)
    assert ids(search(indexed, "calculate_sum", time_range=[before, None])) == [f"{SUM}@v2"]
    assert search(indexed, "calculate_sum", time_range=[None, "2000-01-01"])["layer"] is None


def test_traverse_two_hops_with_fanout(indexed):
    """Criterion: TraverseGraph walks one hop at a time over indexed edge queries, cutting each
    node's edges at `max_fanout`; direction `in` follows edges backwards."""
    walk = traverse(indexed, [f"code/{UTILS}:MathUtils"], hops=2, edge_types=["contains", "invokes"])
    names = {n["id"] for n in walk["nodes"]}
    assert names == {f"code/{UTILS}:MathUtils@v1", f"{SUM}@v1"} | {
        f"code/{UTILS}:MathUtils.{m}@v1" for m in ("calculate_sum", "double", "make", "create")}
    assert {(e["type"], e["src"], e["dst"]) for e in walk["edges"]} >= {
        ("contains", f"code/{UTILS}:MathUtils", DOUBLE),
        ("invokes", f"code/{UTILS}:MathUtils.calculate_sum", SUM),
        ("invokes", DOUBLE, f"code/{UTILS}:MathUtils.calculate_sum")}
    cut = traverse(indexed, [f"code/{UTILS}:MathUtils"], hops=1, edge_types=["contains"], max_fanout=2)
    assert len(cut["edges"]) == 2 and len(cut["nodes"]) == 3
    back = traverse(indexed, [SUM], direction="in")
    assert {(e["type"], e["src"]) for e in back["edges"]} == {
        ("contains", f"code/{UTILS}"), ("imports", "code/main.py"),
        ("invokes", "code/main.py:bare_imported"), ("invokes", f"code/{UTILS}:MathUtils.calculate_sum")}
    assert traverse(indexed, [SUM], node_types=["directory"])["edges"] == []


def test_traverse_supersedes_between_versions(indexed, repo):
    """A `supersedes` edge joins version IDs: from the latest version it reaches the superseded
    one, which is not a latest node, and from a version ID `in` reaches the newer one."""
    edit_sum(repo)
    sync(indexed, "code", repo)
    walk = traverse(indexed, [SUM], edge_types=["supersedes"])
    assert [(n["id"], n["status"]) for n in walk["nodes"]] == [(f"{SUM}@v2", "latest"), (f"{SUM}@v1", "superseded")]
    assert walk["edges"] == [{"type": "supersedes", "src": f"{SUM}@v2", "dst": f"{SUM}@v1"}]
    back = traverse(indexed, [f"{SUM}@v1"], direction="in", edge_types=["supersedes"])
    assert {n["id"] for n in back["nodes"]} == {f"{SUM}@v1", f"{SUM}@v2"}


def test_retrieve_text_lines_and_newest(indexed, repo):
    """Criterion: RetrieveEntity gives the text with its line range, a superseded version points
    to the newest one, and `context_lines` widens a method with lines of its class."""
    edit_sum(repo)
    sync(indexed, "code", repo)
    old, new, unknown = retrieve(indexed, [f"{SUM}@v1", SUM, "code/nope.py"])
    assert (old["status"], old["newest"], old["lines"]) == ("superseded", f"{SUM}@v2", [4, 5])
    assert old["text"].endswith("return a + b") and new["text"].endswith("return b + a") and "newest" not in new
    assert unknown == {"id": "code/nope.py", "error": "unknown ID"}
    with_context, = retrieve(indexed, [DOUBLE], context_lines=1)
    assert with_context["lines"] == [11, 14]
    assert with_context["text"] == "\n    def double(self, a):\n        return self.calculate_sum(a, a)\n"
    top_level, = retrieve(indexed, [SUM], context_lines=3)
    assert top_level["lines"] == [4, 5]   # a file node holds no contiguous text to widen into
    assert retrieve(indexed, [f"code/{UTILS}"], detail="preview")[0]["preview"] == "CONSTANT = 1\n\n\n\n"


def test_retrieve_pages_a_long_text(store, repo):
    (repo / "long.py").write_text("def long():\n" + "".join(f"    x{i} = {i}\n" for i in range(450)))
    sync(store, "code", repo)
    first, = retrieve(store, ["code/long.py:long"])
    assert (first["page"], first["pages"], first["lines"]) == (1, 3, [1, 200])
    assert first["text"].startswith("def long():\n")
    last, = retrieve(store, ["code/long.py:long"], page=3)
    assert (last["page"], last["lines"]) == (3, [401, 451]) and last["text"].endswith("x449 = 449")


def test_by_time(indexed, repo):
    """Criterion: SearchByTime lists what was added, superseded or removed in the range, newest first."""
    before = a_moment_later()
    edit_sum(repo)
    (repo / "pkg/shapes.py").unlink()
    sync(indexed, "code", repo)
    changed = by_time(indexed, [before, None])
    assert {(c["id"], c["status"]) for c in changed} == {
        (f"{SUM}@v2", "latest"), (f"{SUM}@v1", "superseded"),
        ("code/pkg/shapes.py@v1", "removed"), ("code/pkg/shapes.py:Shape@v1", "removed")}
    assert all(c["changed_at"][:19] >= before[:19] for c in changed)
    files = by_time(indexed, [before, None], node_types=["file"])
    assert [(c["id"], c["status"], c["location"]) for c in files] == [("code/pkg/shapes.py@v1", "removed", "pkg/shapes.py:1-3")]
    assert by_time(indexed, [None, "2000-01-01"]) == []
    assert [c["id"] for c in by_time(indexed, [before, None], limit=1)] == [changed[0]["id"]]
    with pytest.raises(ValueError, match="start or an end"):
        by_time(indexed, [None, None])
