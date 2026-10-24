"""MCP server `code-index`: build, search and walk the code graph of a Python repo.

Every tool takes the repo path, a path on this server's host. The index is
read from disk on each call, so it stays as built until `build_index` runs
again. Run: `python -m code_index.server [--host H] [--port P]`; the server is
at /mcp, and when CODE_INDEX_TOKEN is set it asks for `Authorization: Bearer
<token>`. On a host other than loopback it does not start without the token.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import hmac
import os
from typing import Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.responses import PlainTextResponse

from code_index.graph import EDGE_TYPES
from code_index.index import CodeIndex, build_index as build_and_save, load_index

EdgeType = Literal["contains", "imports", "invokes", "inherits"]
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def build_index(repo_path: str) -> dict:
    """Build the code graph and the search index of the Python repo at `repo_path`,
    and save them. Call it again after the code changes."""
    return build_and_save(repo_path)


def search_entities(repo_path: str, query: str,
                    entity_type: Literal["all", "function", "class", "file"] = "all",
                    detail: Literal["fold", "preview", "full"] = "preview") -> dict:
    """Find files, classes and functions by exact ID (`path/file.py:Class.method`),
    else by short name, else by BM25 over the words of the IDs; `entity_type` filters
    only that last layer. The first layer that finds something answers. Test files
    are not searched.
    `detail`: fold is the ID, preview the signature or first lines, full the source."""
    result = load_index(repo_path).search(query, entity_type, detail)
    if result["layer"] is None:
        result["message"] = "no layer matched"
    return result


def traverse_graph(repo_path: str, entity_id: str, edge_types: list[EdgeType] = list(EDGE_TYPES)) -> dict:
    """The entities one outgoing edge away from `entity_id`, by edge type.
    To go further, call it again from one of them."""
    index = load_index(repo_path)
    _node(index, entity_id)
    return {t: [dst for src, dst in index.edges[t] if src == entity_id] for t in edge_types}


def get_entity_code(repo_path: str, entity_id: str) -> dict:
    """The source of one entity, as the last `build_index` saved it."""
    index = load_index(repo_path)
    return {"id": entity_id, **_node(index, entity_id)}


def _node(index: CodeIndex, entity_id: str) -> dict:
    if entity_id not in index.nodes:
        raise ToolError(f"unknown entity ID: {entity_id}; find one with search_entities")
    return index.nodes[entity_id]


def _in_thread(tool):
    """Run a sync tool in a worker thread, so a long build does not hold up other calls."""
    @functools.wraps(tool)
    async def run(**kwargs):
        return await asyncio.to_thread(tool, **kwargs)
    return run


def _require_bearer(app, token: str):
    """Answer 401 to an HTTP request without `Authorization: Bearer <token>`."""
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and not hmac.compare_digest(
                dict(scope["headers"]).get(b"authorization", b""), expected):
            return await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
        await app(scope, receive, send)
    return guarded


def create_app(host: str = "127.0.0.1", token: str | None = None):
    # FastMCP checks the Host header only for a loopback `host`; the gateway
    # reaches a 0.0.0.0 server as host.docker.internal.
    mcp = FastMCP("code-index", host=host)
    for tool in (build_index, search_entities, traverse_graph, get_entity_code):
        mcp.add_tool(_in_thread(tool))
    app = mcp.streamable_http_app()
    return _require_bearer(app, token) if token else app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the code-index MCP server at /mcp.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    token = os.environ.get("CODE_INDEX_TOKEN")
    if not token and args.host not in LOOPBACK_HOSTS:
        parser.error(f"set CODE_INDEX_TOKEN to serve on {args.host}: the server reads any .py file on this host")
    uvicorn.run(create_app(args.host, token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
