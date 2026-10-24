"""The server on a real local port, called with the SDK client that mcp-gateway uses."""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import sys
import threading

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from code_index.graph import build_graph
from code_index.index import index_dir
from code_index import server as code_index_server
from code_index.server import create_app

TOKEN = "test-token"


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
    """Criterion 1: the server runs over HTTP and lists the four tools."""
    async with session(url) as s:
        tools = (await s.list_tools()).tools
    assert sorted(t.name for t in tools) == ["build_index", "get_entity_code", "search_entities", "traverse_graph"]


async def test_refuses_without_token(url):
    """Plan step 3: with CODE_INDEX_TOKEN set, a request without the bearer token gets 401."""
    async with httpx.AsyncClient() as http:
        assert (await http.post(url, json={})).status_code == 401


def test_open_host_needs_token(monkeypatch):
    """On a host other than loopback, the server does not start without a token."""
    monkeypatch.delenv("CODE_INDEX_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["code_index.server", "--host", "0.0.0.0"])
    monkeypatch.setattr(code_index_server.uvicorn, "run", lambda *a, **k: pytest.fail("the server started"))
    with pytest.raises(SystemExit):
        code_index_server.main()


async def test_build_does_not_block_other_calls(url, monkeypatch):
    """A build runs in a worker thread, so other calls are answered meanwhile."""
    started, release = threading.Event(), threading.Event()

    def slow_build(repo_path):
        started.set()
        release.wait(5)
        return {}
    monkeypatch.setattr(code_index_server, "build_and_save", slow_build)
    build = asyncio.create_task(call(url, "build_index", repo_path="x"))
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.01)
            async with session(url) as s:
                assert len((await s.list_tools()).tools) == 4
        assert not build.done()
    finally:
        release.set()
    assert (await build) == (False, {})


async def test_build_saves_index(url, repo):
    """Criterion 2: build_index saves the graph and the index for the repo and gives the counts."""
    is_error, manifest = await call(url, "build_index", repo_path=str(repo))
    assert not is_error
    graph = build_graph(repo)
    assert (manifest["files"], manifest["entities"]) == (10, 20)
    assert manifest["edges"] == sum(map(len, graph["edges"].values()))
    assert {s["file"] for s in manifest["skipped"]} == {"broken.py", "binary.py"}
    folder = index_dir(repo.resolve())
    assert all((folder / name).exists() for name in ("graph.json", "bm25", "manifest.json"))


async def test_traverse_and_get_code(url, repo):
    """Criterion 6: traverse_graph walks one hop of each edge type; get_entity_code gives the source."""
    await call(url, "build_index", repo_path=str(repo))
    _, edges = await call(url, "traverse_graph", repo_path=str(repo), entity_id="pkg/utils.py:MathUtils.double")
    assert edges == {"contains": [], "imports": [], "invokes": ["pkg/utils.py:MathUtils.calculate_sum"],
                     "inherits": []}
    _, edges = await call(url, "traverse_graph", repo_path=str(repo), entity_id="pkg/sub/helpers.py:Fancy",
                          edge_types=["inherits"])
    assert edges == {"inherits": ["pkg/utils.py:MathUtils"]}
    _, entity = await call(url, "get_entity_code", repo_path=str(repo), entity_id="pkg/shapes.py:Shape")
    assert entity["code"] == "class Shape:\n    pass"


async def test_two_repos(url, repo, tmp_path):
    """Criterion 8: each repo has its own index, and a search sees only its own repo."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "other.py").write_text("def calculate_sum():\n    pass\n")
    for path in (repo, other):
        await call(url, "build_index", repo_path=str(path))
    assert index_dir(repo.resolve()) != index_dir(other.resolve())
    _, found = await call(url, "search_entities", repo_path=str(other), query="calculate_sum", detail="fold")
    assert found["results"] == [{"id": "other.py:calculate_sum"}]
    _, found = await call(url, "search_entities", repo_path=str(repo), query="calculate_sum", detail="fold")
    assert len(found["results"]) == 3 and "other.py:calculate_sum" not in {r["id"] for r in found["results"]}


async def test_no_match(url, repo):
    """Criterion 12: a search that no layer matches is a normal result that says so."""
    await call(url, "build_index", repo_path=str(repo))
    is_error, found = await call(url, "search_entities", repo_path=str(repo), query="zzz qqq")
    assert not is_error
    assert (found["layer"], found["results"], found["message"]) == (None, [], "no layer matched")


async def test_bad_repo_paths(url, tmp_path, index_home):
    """Criterion 14: a missing path, a file and a folder with no .py files each give a clear
    error, and no index folder is made."""
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / "empty").mkdir()
    cases = {"missing": "does not exist", "file.txt": "is not a folder", "empty": "no Python files"}
    for name, message in cases.items():
        is_error, text = await call(url, "build_index", repo_path=str(tmp_path / name))
        assert is_error and message in text, name
    assert not index_home.exists()


async def test_not_indexed(url, repo):
    """Criterion 15: search or traverse on a repo never indexed says to run build first."""
    for tool, args in (("search_entities", {"query": "x"}), ("traverse_graph", {"entity_id": "main.py"})):
        is_error, text = await call(url, tool, repo_path=str(repo), **args)
        assert is_error and "run build_index first" in text, tool


async def test_stale_until_rebuild(url, repo):
    """Criterion 16: the index stays as built, with its built_at, until build_index runs again."""
    await call(url, "build_index", repo_path=str(repo))
    with (repo / "pkg/shapes.py").open("a") as f:
        f.write("\n\ndef new_shape():\n    pass\n")
    _, found = await call(url, "search_entities", repo_path=str(repo), query="new_shape", detail="fold")
    assert {"id": "pkg/shapes.py:new_shape"} not in found["results"] and found["built_at"]
    await call(url, "build_index", repo_path=str(repo))
    _, found = await call(url, "search_entities", repo_path=str(repo), query="new_shape", detail="fold")
    assert found["results"] == [{"id": "pkg/shapes.py:new_shape"}]


@pytest.mark.parametrize("damage", [
    lambda folder: (folder / "graph.json").write_text('{"nodes": {'),
    lambda folder: (folder / "graph.json").write_text("{}"),
    lambda folder: shutil.rmtree(folder / "bm25"),
    lambda folder: (folder / "manifest.json").unlink(),
], ids=["graph-truncated", "graph-empty", "bm25-deleted", "manifest-deleted"])
async def test_damaged_index(url, repo, damage):
    """Criterion 17: a missing or damaged index part gives an error that asks for a rebuild."""
    await call(url, "build_index", repo_path=str(repo))
    damage(index_dir(repo.resolve()))
    is_error, text = await call(url, "search_entities", repo_path=str(repo), query="calculate_sum")
    assert is_error and "rebuild" in text
