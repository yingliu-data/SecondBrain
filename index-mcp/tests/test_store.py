"""The store's settings, indexes and document builders, on mongomock (plan step 2)."""
from __future__ import annotations

import pytest

from conftest import NODE_FIELDS
from index_mcp import store as store_module
from index_mcp.store import edge, node, search_index_specs, settings, uri

EDGE_FIELDS = {"type", "src", "dst", "src_path", "active", "created_at", "deactivated_at", "ambiguous"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Credentials come from the variables only: a `.env` on this machine must not leak into a test."""
    monkeypatch.setattr(store_module, "load_dotenv", lambda *a, **k: None)
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


def test_ensure_indexes_creates_the_named_indexes(store):
    store.ensure_indexes()
    assert {"nid_1", "names_1", "is_latest_1_source_1", "path_1", "created_at_1"} <= set(store.nodes.index_information())
    assert {"src_1", "dst_1", "src_path_1", "type_1_active_1"} <= set(store.edges.index_information())
    assert store.files.index_information()["source_1_path_1"]["unique"]
    assert "nid_1" in store.chunks.index_information()


def test_search_index_specs_name_the_three_indexes():
    specs = {spec["name"]: spec for spec in search_index_specs()}
    assert set(specs) == {"nodes_search", "chunks_search", "chunks_vector"}
    assert specs["chunks_vector"]["type"] == "vectorSearch"
    assert specs["chunks_vector"]["definition"]["fields"][0]["model"] == "voyage-4"


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
    doc = edge("imports", "code/main.py", "code/pkg/utils.py", src_path="main.py")
    assert EDGE_FIELDS <= set(doc)
    assert doc["active"] and not doc["ambiguous"] and doc["deactivated_at"] is None
