"""ASGI app: `POST /mcp` (guest, via the forwarder), `/internal/*` (agent-api),
`GET /healthz`. Run: `uvicorn --factory app.main:create_app --port 8090`."""
from __future__ import annotations

import contextlib
import json
import logging
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app import internal, tool_server
from app.config import GatewayConfig, Settings
from app.events import EventPoster
from app.gateway import Gateway
from app.identity import bearer_ok, identity_from_headers

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
# The SDK logs every request at INFO ("Processing request of type ...").
for _noisy in ("mcp.server.lowlevel.server", "mcp.server.streamable_http"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Routes whose callers must stamp X-SB-User/Tenant/Session.
IDENTITY_PATHS = {"/mcp", "/internal/tools"}


class AuthMiddleware:
    """Bearer on every route but /healthz; identity headers on
    IDENTITY_PATHS, trusted only together with the bearer. The parsed
    Identity goes in the scope state (`identity.request_identity`)."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not bearer_ok(headers.get("authorization"), self.token):
            return await _json(send, 401, {"error": "unauthorized"})
        if scope["path"].rstrip("/") in IDENTITY_PATHS:
            ident, err = identity_from_headers(headers)
            if ident is None:
                return await _json(send, 400, {"error": err})
            scope.setdefault("state", {})["identity"] = ident
        return await self.app(scope, receive, send)


async def _json(send: Send, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


class _MCPEndpoint:
    """Starlette treats a class instance endpoint as a raw ASGI app."""

    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


def create_app(settings: Settings | None = None, config: GatewayConfig | None = None,
               events: EventPoster | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    config = config if config is not None else GatewayConfig.load(settings.config_path)
    events = events or EventPoster(settings.agent_api_url, settings.internal_token)
    gateway = Gateway(config, settings, events)
    manager = tool_server.build_session_manager(gateway)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            try:
                yield
            finally:
                await gateway.aclose()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/mcp", _MCPEndpoint(manager), methods=["POST"]),
            *internal.routes(gateway),
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    app.add_middleware(AuthMiddleware, token=settings.internal_token)
    return app

