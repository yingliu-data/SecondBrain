from __future__ import annotations

import threading

import bm25s
import pytest

from code_index.index import TOP_K, build_index, is_test_file, load_index

UTILS = "pkg/utils.py"


@pytest.fixture
def index(repo):
    build_index(str(repo))
    return load_index(str(repo))


def ids(result: dict) -> set[str]:
    return {r["id"] for r in result["results"]}


def test_layer1_exact(index):
    """Criterion 3: an exact ID is found by layer 1, and the search stops there."""
    result = index.search(f"{UTILS}:MathUtils.calculate_sum", detail="fold")
    assert result["layer"] == 1 and result["built_at"]
    assert result["results"] == [{"id": f"{UTILS}:MathUtils.calculate_sum"}]


def test_layer2_name(index):
    """Criteria 3 and 13: a bare name goes to layer 2, which returns every entity with that name."""
    result = index.search("calculate_sum", detail="fold")
    assert result["layer"] == 2
    assert ids(result) == {f"{UTILS}:calculate_sum", f"{UTILS}:MathUtils.calculate_sum",
                           "pkg/sub/tools.py:calculate_sum"}


def test_layer3_bm25_ids(index):
    """Criterion 3: other queries go to BM25 over the ID words, filtered by entity type, top 10."""
    expected = {
        "class": {f"{UTILS}:MathUtils", f"{UTILS}:Base", f"{UTILS}:Child"},
        "function": {f"{UTILS}:calculate_sum"} | {
            f"{UTILS}:MathUtils.{m}" for m in ("calculate_sum", "double", "make", "create")},
        "file": {UTILS},
    }
    expected["all"] = set().union(*expected.values())
    for entity_type, want in expected.items():
        result = index.search("math utils", entity_type, "fold")
        assert (result["layer"], ids(result)) == (3, want), entity_type
    assert index.search("math utils", "class")["results"][0]["id"] == f"{UTILS}:MathUtils"
    assert len(index.search("py")["results"]) == TOP_K


def test_builds_do_not_overlap(repo, monkeypatch):
    """A second build of one repo waits until the first has written its index,
    so their files never mix."""
    entered, release = threading.Event(), threading.Event()
    save = bm25s.BM25.save

    def slow_save(self, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            release.wait(5)
        return save(self, *args, **kwargs)
    monkeypatch.setattr(bm25s.BM25, "save", slow_save)
    builds = [threading.Thread(target=build_index, args=(str(repo),)) for _ in range(2)]
    builds[0].start()
    assert entered.wait(5)
    builds[1].start()
    builds[1].join(0.5)
    assert builds[1].is_alive()
    release.set()
    for build in builds:
        build.join(5)
    assert load_index(str(repo)).search("calculate_sum")["layer"] == 2


def test_detail_levels(index):
    """Criterion 4: fold is the ID, preview the def line or a file's first 5 lines, full the source."""
    nid = f"{UTILS}:MathUtils.double"
    assert index.search(nid, detail="fold")["results"] == [{"id": nid}]
    assert index.search(nid, detail="preview")["results"] == [{"id": nid, "preview": "    def double(self, a):"}]
    assert index.search(nid, detail="full")["results"] == [
        {"id": nid, "code": "    def double(self, a):\n        return self.calculate_sum(a, a)"}]
    assert index.render(f"{UTILS}:MathUtils.make", "preview")["preview"] == "    def make(cls):"
    assert index.search(UTILS)["results"][0]["preview"] == (
        "CONSTANT = 1\n\n\ndef calculate_sum(a, b):\n    return a + b")


def test_test_files_excluded(index):
    """Criterion 5: names that exist only in test files find nothing in layers 1-3,
    and those files are still graph nodes."""
    for query in ("only_in_tests", "only_in_foo_test"):
        assert index.search(query)["layer"] is None, query
    full_id = "tests/test_x.py:only_in_tests"
    assert not any(is_test_file(nid) for nid in ids(index.search(full_id)))
    assert full_id in index.nodes
    paths = ("test/a.py", "a/tests/b.py", "test_a.py", "a/b_test.py", "testing.py", "a/contest.py")
    assert [is_test_file(p) for p in paths] == [True] * 4 + [False] * 2
