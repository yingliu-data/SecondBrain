"""MCP server `index-mcp`: read-only tools over the store that `index_mcp.indexer` writes.

`SearchEntity`, `TraverseGraph`, `RetrieveEntity` and `SearchByTime` answer from
MongoDB through `index_mcp.search`; the server never writes. The store is opened
on the first tool call, and a MongoDB failure becomes a tool error that names it.
Run: `python -m index_mcp.server [--host H] [--port P]`; the server is at /mcp,
and when INDEX_MCP_TOKEN is set it asks for `Authorization: Bearer <token>`.
On a host other than loopback it does not start without the token.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import hmac
import os
import threading
from typing import Annotated, Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from pymongo.errors import OperationFailure, PyMongoError
from starlette.responses import PlainTextResponse

from index_mcp.search import by_time, retrieve, search, traverse
from index_mcp.store import Store

Detail = Literal["fold", "preview", "full"]
Limit = Annotated[int, Field(ge=1, le=100)]
Hops = Annotated[int, Field(ge=1, le=5)]
Fanout = Annotated[int, Field(ge=1, le=100)]
ContextLines = Annotated[int, Field(ge=0, le=100)]
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
AUTHENTICATION_FAILED = 18   # MongoDB's error code

_store: Store | None = None
_store_lock = threading.Lock()


def store() -> Store:
    """The one client of this server, opened on the first tool call."""
    global _store
    with _store_lock:
        if _store is None:
            _store = Store.connect()
        return _store


def search_entity(query: str, scope: str | None = None, node_types: list[str] | None = None,
                  path_prefix: str | None = None, time_range: list[str | None] | None = None,
                  history: bool = False, detail: Detail = "preview", limit: Limit = 10) -> dict:
    """Find files, classes and functions: by exact ID (`code/pkg/utils.py:MathUtils.double`),
    else by name, else by keyword over names, else by content (keyword and meaning). The first
    layer with hits answers. `scope` is the source (`code`); `node_types` filters by type
    (directory, file, class, function); `path_prefix` by path; `time_range` is `[start, end]`
    in ISO 8601, either one null, on when the version was created; `history` also shows
    superseded and removed versions. `detail`: fold is ID, type, location and status; preview
    adds the def line or the first lines; full adds the text."""
    result = search(store(), query, scope=scope, node_types=node_types, path_prefix=path_prefix,
                    time_range=time_range, history=history, detail=detail, limit=limit)
    if result["layer"] is None:
        result["message"] = "no layer matched"
    return result


def traverse_graph(start_ids: list[str], direction: Literal["out", "in", "both"] = "out", hops: Hops = 1,
                   node_types: list[str] | None = None, edge_types: list[str] | None = None,
                   max_fanout: Fanout = 20) -> dict:
    """The entities within `hops` edges of `start_ids` (IDs from SearchEntity), and those edges.
    Edge types: contains, imports, invokes, inherits, supersedes. `out` follows edges from the
    start, `in` edges into it, `both` either way; at most `max_fanout` edges are taken per node."""
    result = traverse(store(), start_ids, direction=direction, hops=hops, node_types=node_types,
                      edge_types=edge_types, max_fanout=max_fanout)
    if not result["nodes"]:
        raise ToolError(f"unknown IDs: {', '.join(start_ids)}; find one with SearchEntity")
    return result


def retrieve_entity(ids: list[str], detail: Detail = "full", context_lines: ContextLines = 0, page: int = 1) -> dict:
    """The text of each ID with its line range: a version ID as it was, a logical ID as it is now.
    A long text comes in pages of 200 lines (`page`); a superseded version names the newest one;
    `context_lines` adds lines around a method from its class."""
    return {"results": retrieve(store(), ids, detail=detail, context_lines=context_lines, page=page)}


def search_by_time(time_range: list[str | None], scope: str | None = None,
                   node_types: list[str] | None = None, limit: Limit = 50) -> dict:
    """What was added, changed or removed in `time_range` (`[start, end]` in ISO 8601, either
    one null), newest change first, with when it changed."""
    return {"results": by_time(store(), time_range, scope=scope, node_types=node_types, limit=limit)}


TOOLS = {"SearchEntity": search_entity, "TraverseGraph": traverse_graph,
         "RetrieveEntity": retrieve_entity, "SearchByTime": search_by_time}


def _in_thread(tool):
    """Run a sync tool in a worker thread, so a slow store query does not hold up other
    calls, and turn a MongoDB failure into a tool error that names it."""
    @functools.wraps(tool)
    async def run(**kwargs):
        try:
            return await asyncio.to_thread(tool, **kwargs)
        except OperationFailure as exc:
            if exc.code == AUTHENTICATION_FAILED:
                raise ToolError("MongoDB authentication failed: check MONGODB_USERNAME and MONGODB_PASSWORD") from exc
            raise ToolError(f"MongoDB query failed: {exc}") from exc
        except PyMongoError as exc:
            raise ToolError(f"MongoDB is not reachable: {exc}") from exc
        except (RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
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
    mcp = FastMCP("index-mcp", host=host)
    for name, tool in TOOLS.items():
        mcp.add_tool(_in_thread(tool), name=name)
    app = mcp.streamable_http_app()
    return _require_bearer(app, token) if token else app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the index-mcp MCP server at /mcp.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    token = os.environ.get("INDEX_MCP_TOKEN")
    if not token and args.host not in LOOPBACK_HOSTS:
        parser.error(f"set INDEX_MCP_TOKEN to serve on {args.host}: the server serves the indexed source code")
    uvicorn.run(create_app(args.host, token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
