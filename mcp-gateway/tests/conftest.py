"""Fixtures: a fake upstream MCP server (SDK FastMCP on a real local port),
a fake agent-api (httpx MockTransport recording gateway events) and the
gateway app driven in-process through ASGITransport with its lifespan."""
from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

from app.config import GatewayConfig, Settings
from app.events import EventPoster
from app.main import create_app

TOKEN = "test-internal-token"
UPSTREAM_TOKEN = "upstream-secret"
IDENT = {"X-SB-User": "alice", "X-SB-Tenant": "wcc-event", "X-SB-Session": "sess_2026-10-09_080000_abcd"}


class Approval(BaseModel):
    approved: bool
    note: str = ""


class FakeUpstream:
    """FastMCP server with echo / add / slow / approve / approve_twice tools."""

    def __init__(self):
        self.mcp = FastMCP("fake-upstream")
        self.auth_headers: list[str | None] = []
        self.release = asyncio.Event()      # lets `slow` finish on demand
        self.elicit_results: list = []
        self.url = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._register()

    def _seen(self, ctx: Context):
        req = ctx.request_context.request
        self.auth_headers.append(req.headers.get("authorization") if req is not None else None)

    def _register(self):
        mcp = self.mcp

        @mcp.tool(description="Echo the text back.")
        async def echo(text: str, ctx: Context) -> str:
            self._seen(ctx)
            return f"echo: {text}"

        @mcp.tool(description="Add two integers.")
        async def add(a: int, b: int) -> str:
            return str(a + b)

        @mcp.tool(description="Wait until released.")
        async def slow(ctx: Context) -> str:
            await self.release.wait()
            return "slow done"

        @mcp.tool(description="Ask for approval, then report it.")
        async def approve(item: str, ctx: Context) -> str:
            res = await ctx.elicit(f"Approve publishing {item}?", Approval)
            self.elicit_results.append(res.action)
            if res.action == "accept":
                return f"{item}: approved={res.data.approved} note={res.data.note}"
            return f"{item}: {res.action}"

        @mcp.tool(description="Ask twice.")
        async def approve_twice(ctx: Context) -> str:
            first = await ctx.elicit("First?", Approval)
            second = await ctx.elicit("Second?", Approval)
            return f"{first.action}/{second.action}"

        @mcp.tool(description="Always fails.")
        async def boom() -> str:
            raise RuntimeError("kaboom")

    def change_echo(self):
        self.mcp.remove_tool("echo")

        @self.mcp.tool(description="Echo the text back, LOUDLY.")
        async def echo(text: str) -> str:
            return f"ECHO: {text.upper()}"

    def add_tool(self):
        @self.mcp.tool(description="A tool added later.")
        async def newcomer() -> str:
            return "new"

    async def start(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(self.mcp.streamable_http_app(), log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            await asyncio.sleep(0.01)
        self.url = f"http://127.0.0.1:{port}/mcp"

    async def stop(self):
        self.release.set()
        if self._server:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 5)


class FakeAgentAPI:
    def __init__(self):
        self.events: list[dict] = []
        self.auth: list[str | None] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json
        assert request.url.path == "/internal/gateway-events"
        if self.fail:
            return httpx.Response(500)
        self.auth.append(request.headers.get("authorization"))
        self.events.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def ops(self, op: str) -> list[dict]:
        return [e for e in self.events if e["op"] == op]


@pytest.fixture
async def upstream():
    up = FakeUpstream()
    await up.start()
    yield up
    await up.stop()


@pytest.fixture
def agent_api():
    return FakeAgentAPI()


def make_config(upstream_url: str, **tenant_overrides) -> GatewayConfig:
    tenant = {"upstreams": ["fake"], "allow": ["*"], "deny": ["fake__boom_hidden"],
              "promoted": ["fake__echo"], "max_concurrent": 2}
    tenant.update(tenant_overrides)
    return GatewayConfig.from_dict({
        "upstreams": {"fake": {"url": upstream_url, "auth_token": UPSTREAM_TOKEN, "timeout_s": 30,
                               "tool_defs_ttl_s": 300, "allow_private": True}},
        "tenants": {"wcc-event": tenant},
    })


class GatewayHarness:
    """One gateway 'process': app + lifespan + an httpx client."""

    def __init__(self, config: GatewayConfig, state_dir, agent_api: FakeAgentAPI, token: str = TOKEN):
        self.settings = Settings(config_path="/nonexistent", state_dir=str(state_dir),
                                 agent_api_url="http://agent-api:8000", internal_token=token,
                                 allowed_private_hosts=[])
        self.events = EventPoster(self.settings.agent_api_url, token,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(agent_api.handler)))
        self.app = create_app(self.settings, config, self.events)
        self._stack = contextlib.AsyncExitStack()
        self.client: httpx.AsyncClient | None = None
        self._id = 0

    @property
    def gateway(self):
        return self.app.state.gateway

    async def __aenter__(self):
        await self._stack.enter_async_context(self.app.router.lifespan_context(self.app))
        self.client = await self._stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://gateway", timeout=60))
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    def headers(self, ident: dict | None = None, token: str | None = TOKEN) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        h.update(IDENT if ident is None else ident)
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def rpc(self, method: str, params: dict | None = None, *, ident: dict | None = None,
                  token: str | None = TOKEN) -> httpx.Response:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        return await self.client.post("/mcp", json=body, headers=self.headers(ident, token))

    async def call(self, name: str, arguments: dict | None = None, meta: dict | None = None,
                   ident: dict | None = None) -> dict:
        params = {"name": name, "arguments": arguments or {}}
        if meta is not None:
            params["_meta"] = meta
        r = await self.rpc("tools/call", params, ident=ident)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "result" in body, body
        return body["result"]

    async def internal(self, method: str, path: str, *, ident: dict | None = None,
                       token: str | None = TOKEN, **kw) -> httpx.Response:
        h = {}
        if ident is not False:
            h.update(IDENT if ident is None else ident)
        if token:
            h["Authorization"] = f"Bearer {token}"
        return await self.client.request(method, path, headers=h, **kw)


@pytest.fixture
def harness(tmp_path, agent_api):
    def _make(config: GatewayConfig, **kw) -> GatewayHarness:
        return GatewayHarness(config, tmp_path / "state", agent_api, **kw)
    return _make


def text_of(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result["content"])
