"""`/internal/*` routes. The bearer and identity checks
run in `main.AuthMiddleware` before these handlers.

The legacy engine's routes (`GET /internal/upstreams/{name}/tools`,
`POST /internal/call`) are deleted; tool calls arrive over `POST /mcp` (the
guest, through agent-api's forwarder)."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from protocol.mcp import INTERNAL_TOOLS_PATH

from app.gateway import Gateway
from app.identity import request_identity


def routes(gateway: Gateway) -> list[Route]:
    async def tools(request: Request) -> JSONResponse:
        ident = request_identity(request)
        return JSONResponse({"tools": await gateway.hello_entries(ident)})

    return [Route(INTERNAL_TOOLS_PATH, tools, methods=["GET"])]
