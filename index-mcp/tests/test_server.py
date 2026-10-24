"""The server on a real local port, called with the SDK client that mcp-gateway uses."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pymongo.errors import OperationFailure

from index_mcp import server as index_mcp_server
from index_mcp import store as store_module
from index_mcp.indexer import sync
from index_mcp.server import create_app

TOKEN = "test-token"
TOOL_CALLS = (("SearchEntity", {"query": "x"}), ("TraverseGraph", {"start_ids": ["x"]}),
              ("RetrieveEntity", {"ids": ["x"]}), ("SearchByTime", {"time_range": ["2000-01-01", None]}))
OUT_OF_BOUNDS = (("SearchEntity", {"query": "x", "limit": 0}), ("TraverseGraph", {"start_ids": ["x"], "hops": 6}),
                 ("TraverseGraph", {"start_ids": ["x"], "max_fanout": 0}), ("RetrieveEntity", {"ids": ["x"], "context_lines": -1}),
                 ("SearchByTime", {"time_range": ["2000-01-01", None], "limit": 101}))


@pytest.fixture
async def url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(token=TOKEN), log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(5):
        while not server.started:
            await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    server.should_exit = True
    await asyncio.wait_for(task, 5)


@pytest.fixture
def served(store, repo, monkeypatch):
    """The fake store, synced with the fixture repo, as the server's store."""
    sync(store, "code", repo)
    monkeypatch.setattr(index_mcp_server, "_store", store)
    return store


@contextlib.asynccontextmanager
async def session(url: str):
    async with (httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
                streamable_http_client(url, http_client=http) as (read, write, _),
                ClientSession(read, write) as s):
        await s.initialize()
        yield s


async def call(url: str, tool: str, **args) -> tuple[bool, dict | str]:
    """(isError, the error text or the parsed result)."""
    async with session(url) as s:
        result = await s.call_tool(tool, args)
    text = result.content[0].text
    return result.isError, text if result.isError else json.loads(text)


async def test_lists_four_tools(url):
    """Criterion: the server exposes exactly SearchEntity, TraverseGraph, RetrieveEntity and SearchByTime."""
    async with session(url) as s:
        tools = (await s.list_tools()).tools
    assert sorted(t.name for t in tools) == ["RetrieveEntity", "SearchByTime", "SearchEntity", "TraverseGraph"]


async def test_tools_answer_from_the_store(url, served):
    """Each tool answers through the store, in the one output shape; a bad ID or range is a clear error."""
    is_error, found = await call(url, "SearchEntity", query="calculate_sum", detail="fold")
    assert not is_error and found["layer"] == 2 and len(found["results"]) == 3
    assert set(found["results"][0]) == {"id", "type", "location", "status", "heading_path"}
    _, none = await call(url, "SearchEntity", query="zzz qqq")
    assert (none["layer"], none["message"]) == (None, "no layer matched")

    _, walk = await call(url, "TraverseGraph", start_ids=["code/pkg/utils.py:MathUtils.double"], edge_types=["invokes"])
    assert {e["dst"] for e in walk["edges"]} == {"code/pkg/utils.py:MathUtils.calculate_sum"}
    is_error, text = await call(url, "TraverseGraph", start_ids=["code/nope.py"])
    assert is_error and "unknown IDs: code/nope.py" in text

    _, got = await call(url, "RetrieveEntity", ids=["code/pkg/shapes.py:Shape"])
    assert got["results"][0]["text"] == "class Shape:\n    pass" and got["results"][0]["lines"] == [2, 3]

    _, changed = await call(url, "SearchByTime", time_range=["2000-01-01", None], node_types=["file"])
    assert len(changed["results"]) == 10
    is_error, text = await call(url, "SearchByTime", time_range=[None, None])
    assert is_error and "start or an end" in text


async def test_out_of_bounds_input_is_a_clear_error(url, served):
    """A `limit`, `hops`, `max_fanout` or `context_lines` outside its bounds is refused with an
    error that names the argument, before any query runs; the schema tells the client the bounds."""
    for tool, args in OUT_OF_BOUNDS:
        is_error, text = await call(url, tool, **args)
        bad, = set(args) - {"query", "start_ids", "ids", "time_range"}
        assert is_error and bad in text and "MongoDB" not in text, (tool, args)
    async with session(url) as s:
        schemas = {t.name: t.inputSchema["properties"] for t in (await s.list_tools()).tools}
    assert (schemas["SearchEntity"]["limit"]["minimum"], schemas["SearchEntity"]["limit"]["maximum"]) == (1, 100)
    assert schemas["TraverseGraph"]["hops"]["maximum"] == 5 and schemas["RetrieveEntity"]["context_lines"]["minimum"] == 0


async def test_refuses_without_token(url):
    """With INDEX_MCP_TOKEN set, a request without the bearer token gets 401."""
    async with httpx.AsyncClient() as http:
        assert (await http.post(url, json={})).status_code == 401


def test_open_host_needs_token(monkeypatch):
    """On a host other than loopback, the server does not start without a token."""
    monkeypatch.delenv("INDEX_MCP_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["index_mcp.server", "--host", "0.0.0.0"])
    monkeypatch.setattr(index_mcp_server.uvicorn, "run", lambda *a, **k: pytest.fail("the server started"))
    with pytest.raises(SystemExit):
        index_mcp_server.main()


async def test_slow_tool_does_not_block_other_calls(url, served, monkeypatch):
    """A tool runs in a worker thread, so other calls are answered meanwhile."""
    started, release = threading.Event(), threading.Event()

    def slow_search(*args, **kwargs):
        started.set()
        release.wait(5)
        return {"layer": None, "results": []}
    monkeypatch.setattr(index_mcp_server, "search", slow_search)
    pending = asyncio.create_task(call(url, "SearchEntity", query="x"))
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.01)
            async with session(url) as s:
                assert len((await s.list_tools()).tools) == 4
        assert not pending.done()
    finally:
        release.set()
    assert (await pending) == (False, {"layer": None, "results": [], "message": "no layer matched"})


async def test_mongodb_down(url, monkeypatch):
    """Criterion: with MongoDB unreachable, or the credentials missing or wrong, `list_tools` still
    works and each tool returns an error that names MongoDB and the reason; the server does not crash."""
    monkeypatch.setattr(index_mcp_server, "_store", None)
    monkeypatch.setattr(store_module, "load_dotenv", lambda *a, **k: None)
    for name, value in {"MONGODB_USERNAME": "u", "MONGODB_PASSWORD": "p", "MONGODB_HOST": "127.0.0.1"}.items():
        monkeypatch.setenv(name, value)
    async with session(url) as s:
        assert len((await s.list_tools()).tools) == 4
    for tool, args in TOOL_CALLS:
        is_error, text = await call(url, tool, **args)
        assert is_error and "MongoDB is not reachable: " in text, tool

    monkeypatch.delenv("MONGODB_PASSWORD")
    is_error, text = await call(url, "SearchEntity", query="x")
    assert is_error and "set MONGODB_PASSWORD" in text

    def wrong_password(*args, **kwargs):
        raise OperationFailure("Authentication failed.", code=18)
    monkeypatch.setattr(index_mcp_server, "search", wrong_password)
    monkeypatch.setattr(index_mcp_server, "_store", object())
    is_error, text = await call(url, "SearchEntity", query="x")
    assert is_error and "MongoDB authentication failed" in text


def test_server_has_no_write_path():
    """Criterion: the server is read-only against MongoDB; it imports the search and the store, not the indexer."""
    tree = ast.parse(Path(index_mcp_server.__file__).read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "index_mcp.indexer" not in imported and {"index_mcp.search", "index_mcp.store"} <= imported
