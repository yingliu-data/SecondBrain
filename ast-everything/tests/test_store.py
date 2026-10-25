"""The store's settings, indexes and document builders, on mongomock (plan step 2)."""
from __future__ import annotations

import pytest

from conftest import NODE_FIELDS
from ast_everything import store as store_module
from ast_everything.store import Store, edge, node, search_index_specs, settings, uri

EDGE_FIELDS = {"type", "src", "dst", "source", "src_path", "active", "created_at", "deactivated_at", "ambiguous"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Credentials come from the variables only: a `.env` on this machine must not leak into a test."""
    monkeypatch.setattr(store_module, "find_dotenv", lambda *a, **k: "")
    monkeypatch.setenv("MONGODB_USERNAME", "bob")
    monkeypatch.setenv("MONGODB_PASSWORD", "p@ss/w:rd")
    monkeypatch.setenv("MONGODB_HOST", "cluster.example.net")
    monkeypatch.delenv("MONGODB_DB", raising=False)


def test_uri_quotes_the_password():
    """Criterion: credentials come from `.env`; the password is URL-quoted and the database defaults."""
    values = settings()
    assert uri(values) == "mongodb+srv://bob:p%40ss%2Fw%3Ard@cluster.example.net/"
    assert values["MONGODB_DB"] == "secondbrain"


def test_missing_variable_is_named(monkeypatch):
    monkeypatch.delenv("MONGODB_PASSWORD")
    with pytest.raises(RuntimeError, match="MONGODB_PASSWORD"):
        settings()


def test_dotenv_is_read_but_not_loaded_into_the_environment(monkeypatch, tmp_path):
    """A `.env` fills what the environment lacks, and the environment stays as it was, so a
    process started from this one (a local guest) does not get the password."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("MONGODB_PASSWORD=from-file\nMONGODB_DB=notes\n")
    monkeypatch.setattr(store_module, "find_dotenv", lambda *a, **k: str(dotenv))
    monkeypatch.delenv("MONGODB_PASSWORD")
    values = settings()
    assert (values["MONGODB_PASSWORD"], values["MONGODB_DB"], values["MONGODB_USERNAME"]) == ("from-file", "notes", "bob")
    assert "MONGODB_PASSWORD" not in store_module.os.environ and "MONGODB_DB" not in store_module.os.environ


def test_ensure_indexes_creates_the_named_indexes(store):
    store.ensure_indexes()
    assert {"nid_1", "names_1", "is_latest_1_source_1", "path_1", "created_at_1", "superseded_at_1", "removed_at_1"} <= set(
        store.nodes.index_information())
    assert {"src_1", "dst_1", "source_1_src_path_1", "type_1_active_1"} <= set(store.edges.index_information())
    assert store.files.index_information()["source_1_path_1"]["unique"]
    assert "nid_1" in store.chunks.index_information()


def test_search_index_specs_name_the_three_indexes():
    specs = {spec["name"]: spec for spec in search_index_specs()}
    assert set(specs) == {"nodes_search", "chunks_search", "chunks_vector"}
    assert specs["chunks_vector"]["type"] == "vectorSearch"
    assert specs["chunks_vector"]["definition"]["fields"][0]["model"] == "voyage-4"


def test_vector_stage_takes_the_limit_as_is_within_the_cap(store):
    """`$vectorSearch` gets the caller's limit, not an over-fetch of it, and at most 10000 candidates;
    only `is_latest` and `source` go into its own filter."""
    pipelines = []
    store.chunks.aggregate = lambda pipeline: pipelines.append(pipeline) or []
    for limit in (50, 2000):
        Store.vector(store, "q", {"is_latest": True, "path": {"$regex": "^pkg"}}, limit)
    stages = [pipeline[0]["$vectorSearch"] for pipeline in pipelines]
    assert [(stage["limit"], stage["numCandidates"]) for stage in stages] == [(50, 500), (2000, 10000)]
    assert stages[0]["filter"] == {"is_latest": True}


def test_a_hidden_source_is_matched_after_the_search_stage(store):
    """Issue #56, review round 2, request 1: a `$nin` on `source` is no Atlas Search filter;
    the `$match` after the stage applies it. Review round 4, request 2: `$vectorSearch` filters
    on it before its limit, so hidden chunks that rank higher take no candidate."""
    pipelines = []
    store.chunks.aggregate = lambda pipeline: pipelines.append(pipeline) or []
    match = {"is_latest": True, "source": {"$nin": ["memory-bob"]}}
    Store.vector(store, "q", match, 10)
    Store.keyword_text(store, "q", match, 10)
    vector, keyword = pipelines
    assert vector[0]["$vectorSearch"]["filter"] == match
    assert keyword[0]["$search"]["compound"]["filter"] == [{"equals": {"path": "is_latest", "value": True}}]
    assert vector[1] == {"$addFields": {"score": {"$meta": "vectorSearchScore"}}}
    assert vector[2] == keyword[1] == {"$match": match}


def test_node_builder_sets_every_common_field():
    """Criterion: code and doc nodes share one schema; the ID is `<nid>@v<n>`."""
    doc = node("code/pkg/utils.py:MathUtils.double", type="function", source="code", path="pkg/utils.py",
               title="double", heading_path=["MathUtils", "double"], names=["double", "MathUtils.double"],
               start_line=10, end_line=12, text="def double(self, a):\n", version=2)
    assert NODE_FIELDS <= set(doc)
    assert doc["_id"] == "code/pkg/utils.py:MathUtils.double@v2"
    assert doc["stem"] == "utils"
    assert doc["is_latest"] and doc["superseded_by"] is None and doc["removed_at"] is None
    assert doc["text_hash"] == node("x", type="file", source="code", path="x", title="x", heading_path=[],
                                    names=[], start_line=1, end_line=1, text="def double(self, a):\n")["text_hash"]


def test_edge_builder_sets_every_common_field():
    doc = edge("imports", "code/main.py", "code/pkg/utils.py", source="code", src_path="main.py")
    assert EDGE_FIELDS <= set(doc)
    assert doc["active"] and not doc["ambiguous"] and doc["deactivated_at"] is None
