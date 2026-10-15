"""The guest-facing MCP server on `POST /mcp`.

Low-level `mcp.server.lowlevel.Server` behind a stateless, JSON-response
`StreamableHTTPSessionManager`: no `initialize` needed (stateless sessions
start Initialized), each POST is one JSON-RPC request → one JSON body.

The handlers are registered straight into `Server.request_handlers` rather
than through the `@server.call_tool()` decorator: the decorator validates
arguments against a tool cache it fills by calling the list handler, and
wraps the result; we need the raw `params._meta` (for `sb/input_response`)
and to return a `CallToolResult` whose `_meta` carries `sb/input_required`.
The identity comes from the Starlette request the transport attaches to
each message (`request_context.request`): `main.AuthMiddleware` validated it
and stored it in the request's scope state.
"""
from __future__ import annotations

import logging

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from app.gateway import Gateway, error_result
from app.identity import request_identity

logger = logging.getLogger("gateway.tool_server")


def build_server(gateway: Gateway) -> Server:
    server: Server = Server("secondbrain-mcp-gateway")

    def _ident():
        request = server.request_context.request
        if request is None:
            raise ValueError("no HTTP request for this message")
        return request_identity(request)

    async def list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
        tools = await gateway.mcp_tools(_ident())
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        ident = _ident()
        meta = req.params.meta.model_dump(by_alias=True, exclude_none=True) if req.params.meta else None
        try:
            result = await gateway.call_mcp(ident, req.params.name, req.params.arguments, meta)
        except Exception as e:  # noqa: BLE001 — gateway failures are results, never JSON-RPC errors
            logger.exception("tools/call %s failed", req.params.name)
            result = error_result(f"gateway error: {e}")
        return types.ServerResult(result)

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
    return server


def build_session_manager(gateway: Gateway) -> StreamableHTTPSessionManager:
    return StreamableHTTPSessionManager(app=build_server(gateway), json_response=True, stateless=True)
