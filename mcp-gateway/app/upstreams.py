"""Upstream MCP clients (moved from agent-api app/mcp/client.py).

One short-lived streamable-HTTP session per operation: holding a
ClientSession across requests fights the SDK's anyio cancel-scope semantics,
and call volume is chat-paced. Tool definitions are cached per upstream for
`tool_defs_ttl_s`.

`UpstreamCall` runs one tools/call in its own asyncio task so it can outlive
the guest's HTTP request: when the upstream elicits, the callback parks on a
future and the gateway answers the guest with `sb/input_required`
(M4-CONTRACTS.md §3.2); the next POST feeds the answer in.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from app import ssrf
from app.config import UpstreamConfig

logger = logging.getLogger("gateway.upstreams")

CONNECT_TIMEOUT_S = 30
ABANDON_GRACE_S = 30          # after an expired/abandoned hold, how long the upstream may wind down


def unwrap(e: BaseException) -> str:
    """anyio task groups wrap the real failure in ExceptionGroups — surface
    the first leaf exception so error strings stay readable."""
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


def _http_client(cfg: UpstreamConfig, read_timeout_s: float) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else None
    # No redirects: a redirect would bypass the SSRF check on the configured URL.
    return httpx.AsyncClient(
        headers=headers, follow_redirects=False,
        timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=read_timeout_s),
    )


ElicitFn = Callable[[types.ElicitRequestParams], Awaitable[types.ElicitResult]]


class Upstream:
    def __init__(self, cfg: UpstreamConfig, allowed_private_hosts: list[str]):
        self.cfg = cfg
        self._allowed_private_hosts = allowed_private_hosts
        self._tools: list[types.Tool] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.cfg.name

    def check_url(self) -> None:
        ssrf.validate_mcp_url(self.cfg, self._allowed_private_hosts)

    def _fresh(self) -> bool:
        return self._tools is not None and time.monotonic() - self._fetched_at < self.cfg.tool_defs_ttl_s

    async def list_tools(self) -> list[types.Tool]:
        """Cached tools/list. A failed refresh serves the stale list when there
        is one, else raises."""
        if self._fresh():
            return self._tools  # type: ignore[return-value]
        async with self._lock:
            if self._fresh():
                return self._tools  # type: ignore[return-value]
            try:
                tools = await self._fetch_tools()
            except Exception as e:
                if self._tools is not None:
                    logger.warning("upstream %s: tools/list failed, serving stale cache: %s", self.name, unwrap(e))
                    return self._tools
                raise
            self._tools, self._fetched_at = tools, time.monotonic()
            logger.info("upstream %s: %d tools cached", self.name, len(tools))
            return tools

    def invalidate(self) -> None:
        self._fetched_at = 0.0

    async def _fetch_tools(self) -> list[types.Tool]:
        self.check_url()
        tools: list[types.Tool] = []
        async with _http_client(self.cfg, CONNECT_TIMEOUT_S) as http:
            async with streamable_http_client(self.cfg.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    cursor = None
                    while True:
                        page = await session.list_tools(
                            params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                        tools.extend(page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            return tools

    def start_call(self, tool: str, arguments: dict, *, relay: bool, hold_s: float) -> "UpstreamCall":
        return UpstreamCall(self, tool, arguments, relay=relay, hold_s=hold_s)


class UpstreamCall:
    """One upstream tools/call running in its own task.

    Outcomes arrive on a queue: ("result", CallToolResult), ("input",
    ElicitRequestParams) or ("error", str). With relay=False an elicitation
    is answered `decline` at once (the legacy route, §3)."""

    def __init__(self, upstream: Upstream, tool: str, arguments: dict, *, relay: bool, hold_s: float):
        self.upstream = upstream
        self.tool = tool
        self.started = time.monotonic()
        self.deadline = self.started + upstream.cfg.timeout_s
        self._relay = relay
        self._hold_s = hold_s
        self._outcomes: asyncio.Queue = asyncio.Queue()
        self._answer: asyncio.Future | None = None
        self._abandoned = False
        self.task = asyncio.create_task(self._run(arguments), name=f"upstream-call:{upstream.name}/{tool}")

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def _elicit(self, _ctx, params: types.ElicitRequestParams) -> types.ElicitResult:
        if not self._relay or self._abandoned:
            return types.ElicitResult(action="decline" if not self._relay else "cancel")
        self._answer = asyncio.get_running_loop().create_future()
        self._outcomes.put_nowait(("input", params))
        try:
            return await self._answer
        finally:
            self._answer = None

    async def _run(self, arguments: dict) -> None:
        cfg = self.upstream.cfg
        try:
            async with _http_client(cfg, cfg.timeout_s + self._hold_s) as http:
                async with streamable_http_client(cfg.url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write, elicitation_callback=self._elicit) as session:
                        await session.initialize()
                        result = await session.call_tool(self.tool, arguments)
            self._outcomes.put_nowait(("result", result))
        except asyncio.CancelledError:
            self._outcomes.put_nowait(("error", "upstream call cancelled"))
            raise
        except BaseException as e:  # noqa: BLE001 — anyio ExceptionGroups
            reason = unwrap(e)
            logger.error("upstream call failed [%s/%s]: %s", cfg.name, self.tool, reason)
            self._outcomes.put_nowait(("error", f"MCP server '{cfg.name}' unreachable or failed: {reason}"))

    async def next_outcome(self) -> tuple[str, object]:
        """Wait for the next outcome within the call's remaining budget.
        On timeout the upstream call is cancelled."""
        try:
            return await asyncio.wait_for(self._outcomes.get(), timeout=self.remaining_s())
        except (asyncio.TimeoutError, TimeoutError):
            self.task.cancel()
            return ("timeout", f"tool '{self.tool}' on MCP server '{self.upstream.name}' "
                               f"timed out after {self.upstream.cfg.timeout_s}s")

    def answer(self, result: types.ElicitResult) -> bool:
        if self._answer is None or self._answer.done():
            return False
        self._answer.set_result(result)
        return True

    def abandon(self) -> None:
        """No one will read further outcomes: cancel any pending question,
        refuse later ones, and cut the task after a grace period."""
        self._abandoned = True
        self.answer(types.ElicitResult(action="cancel"))
        if not self.task.done():
            asyncio.get_running_loop().call_later(ABANDON_GRACE_S, self.task.cancel)
