"""The real store: Atlas Search, automated embedding and vector search over the fixture repo.

Needs the `MONGODB_*` variables (`.env`) and a cluster that allows the Atlas Search
indexes of `atlas/*.json`. Run with `INDEX_MCP_LIVE=1`; skipped otherwise. It writes
into a throw-away database named after the run and drops it at the end.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
from pymongo import MongoClient

from index_mcp.indexer import sync
from index_mcp.search import search
from index_mcp.store import Store, settings, uri

pytestmark = pytest.mark.skipif(os.environ.get("INDEX_MCP_LIVE") != "1", reason="set INDEX_MCP_LIVE=1 to run against Atlas")

SUM = "code/pkg/utils.py:calculate_sum@v1"
WAIT_S = 600


@pytest.fixture
def live_store():
    values = settings()
    client = MongoClient(uri(values), tz_aware=True)
    name = f"index_mcp_test_{uuid.uuid4().hex[:8]}"
    store = Store(client, name)
    store.ensure_indexes()
    store.ensure_search_indexes()
    yield store
    client.drop_database(name)


def wait_until(check, what: str):
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(5)
    pytest.fail(f"gave up after {WAIT_S}s waiting for {what}")


def test_atlas_embeds_each_chunk_once_and_searches(live_store, repo, monkeypatch):
    """Criteria: each chunk is embedded once (a second sync adds no chunk and vector search
    returns every chunk), layer 3 and layer 4 run on Atlas indexes, and with vector search
    down the answer still comes by keyword."""
    sync(live_store, "code", repo)
    chunks = live_store.chunks.count_documents({})
    assert chunks == live_store.nodes.count_documents({"type": {"$in": ["class", "function"]}})

    def ready(index):
        status = next((i for i in live_store.chunks.list_search_indexes() if i["name"] == index), {})
        return status.get("queryable") and status.get("status") == "READY"
    wait_until(lambda: ready("chunks_vector") and ready("chunks_search"), "the chunk indexes")
    wait_until(lambda: len(live_store.vector("add two numbers", {"is_latest": True}, chunks)) == chunks,
               "one embedding per chunk")
    assert len({c["_id"] for c in live_store.vector("add two numbers", {"is_latest": True}, chunks)}) == chunks

    sync(live_store, "code", repo)
    assert live_store.chunks.count_documents({}) == chunks

    wait_until(lambda: live_store.keyword_names("calculate", {"is_latest": True}), "the nodes index")
    assert search(live_store, "calculate", detail="fold")["layer"] == 3
    assert search(live_store, "calculate", history=True, detail="fold")["layer"] == 3
    found = search(live_store, "the sum of two numbers", detail="fold")
    assert found["layer"] == 4 and SUM in {r["id"] for r in found["results"]}

    def broken(*args, **kwargs):
        raise RuntimeError("vector search is off")
    monkeypatch.setattr(live_store, "vector", broken)
    found = search(live_store, "return a + b", detail="fold")
    assert found["layer"] == 4 and SUM in {r["id"] for r in found["results"]}
