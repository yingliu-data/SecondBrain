"""Upstream MCP clients (moved from agent-api app/mcp/client.py).

One streamable-HTTP client session per operation: holding a ClientSession
across requests fights the SDK's anyio cancel-scope semantics, and call volume
is chat-paced. The httpx clients under them are long-lived (one per upstream
for tools/list, one for tools/call), so connections are pooled.

Tool definitions are cached per upstream as {tool: (defn, pin)}, computed once
per fetch. An expired cache is served while one background refresh runs; a
failed first fetch is remembered for FAILURE_BACKOFF_S, during which callers
fail at once instead of reconnecting.

`UpstreamCall` runs one tools/call in its own asyncio task so it can outlive
the guest's HTTP request: when the upstream elicits, the callback parks on a
future and the gateway answers the guest with `sb/input_required`;
the next POST feeds the answer in.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from protocol.mcp import bearer_header

from app import ssrf
from app.config import UpstreamConfig
from app.pins import pin_of
from app.schema import tool_defn

logger = logging.getLogger("gateway.upstreams")

CONNECT_TIMEOUT_S = 30
ABANDON_GRACE_S = 30          # after an expired/abandoned hold, how long the upstream may wind down
FAILURE_BACKOFF_S = 30        # a failed tools/list is not retried for this long

ToolIndex = dict[str, tuple[dict, str]]   # un-namespaced tool name -> (defn, pin)


class UpstreamUnavailable(Exception):
    pass


def unwrap(e: BaseException) -> str:
    """anyio task groups wrap the real failure in ExceptionGroups — surface
    the first leaf exception so error strings stay readable."""
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


def _http_client(cfg: UpstreamConfig, read_timeout_s: float) -> httpx.AsyncClient:
    headers = bearer_header(cfg.auth_token) or None
    # No redirects: a redirect would bypass the SSRF check on the configured URL.
    return httpx.AsyncClient(
        headers=headers, follow_redirects=False,
        timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=read_timeout_s),
    )


class Upstream:
    def __init__(self, cfg: UpstreamConfig, allowed_private_hosts: list[str], hold_s: float):
        self.cfg = cfg
        self._allowed_private_hosts = allowed_private_hosts
        self.list_http = _http_client(cfg, CONNECT_TIMEOUT_S)
        # A call's SSE stream stays open while it waits for the guest's answer.
        self.call_http = _http_client(cfg, cfg.timeout_s + hold_s)
        self._tools: ToolIndex | None = None
        self._fetched_at = 0.0
        self._failed_at: float | None = None
        self._last_error = ""
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self.cfg.name

    def check_url(self) -> None:
        ssrf.validate_mcp_url(self.cfg, self._allowed_private_hosts)

    def _backing_off(self) -> bool:
        return self._failed_at is not None and time.monotonic() - self._failed_at < FAILURE_BACKOFF_S

    async def tools(self, fresh: bool = False) -> ToolIndex:
        """Cached tools/list. Expired → the cached index, plus one background
        refresh. Never fetched → fetched now (or the recent failure raised).

        `fresh=True` (pin time: a session's surface is fixed by what it sees
        now) waits for the refresh when the cache has expired, so a new
        session never pins a definition the upstream already replaced; if
        the refresh fails it falls back to the cached index."""
        if self._tools is not None:
            expired = time.monotonic() - self._fetched_at >= self.cfg.tool_defs_ttl_s
            idle = self._refresh_task is None or self._refresh_task.done()
            if expired and idle and not self._backing_off():
                self._refresh_task = asyncio.create_task(self._refresh_quietly())
            if fresh and expired and self._refresh_task is not None and not self._refresh_task.done():
                await asyncio.shield(self._refresh_task)
            return self._tools
        async with self._lock:
            if self._tools is None:
                await self.refresh()
            return self._tools  # type: ignore[return-value]

    async def _refresh_quietly(self) -> None:
        try:
            await self.refresh()
        except Exception as e:  # noqa: BLE001
            logger.warning("upstream %s: tools/list failed, serving stale cache: %s", self.name, unwrap(e))

    async def refresh(self) -> ToolIndex:
        """Fetch tools/list now and replace the cache."""
        if self._backing_off():
            raise UpstreamUnavailable(f"{self._last_error} (not retried for {FAILURE_BACKOFF_S}s)")
        try:
            listed = await self._fetch_tools()
        except Exception as e:
            self._failed_at, self._last_error = time.monotonic(), unwrap(e)
            raise
        index: ToolIndex = {}
        for t in listed:
            defn = tool_defn(t)
            index[t.name] = (defn, pin_of(defn))
        self._tools, self._fetched_at, self._failed_at = index, time.monotonic(), None
        logger.info("upstream %s: %d tools cached", self.name, len(index))
        return index

    async def _fetch_tools(self) -> list[types.Tool]:
        self.check_url()
        tools: list[types.Tool] = []
        async with streamable_http_client(self.cfg.url, http_client=self.list_http) as (read, write, _):
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

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
        await self.list_http.aclose()
        await self.call_http.aclose()


class UpstreamCall:
    """One upstream tools/call running in its own task.

    Outcomes arrive on a queue: ("result", CallToolResult), ("input",
    ElicitRequestParams) or ("error", str)."""

    def __init__(self, upstream: Upstream, tool: str, arguments: dict):
        self.upstream = upstream
        self.tool = tool
        self.started = time.monotonic()
        self.deadline = self.started + upstream.cfg.timeout_s
        self._outcomes: asyncio.Queue = asyncio.Queue()
        self._answer: asyncio.Future | None = None
        self._abandoned = False
        self.task = asyncio.create_task(self._run(arguments), name=f"upstream-call:{upstream.name}/{tool}")

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def _elicit(self, _ctx, params: types.ElicitRequestParams) -> types.ElicitResult:
        if self._abandoned:
            return types.ElicitResult(action="cancel")
        self._answer = asyncio.get_running_loop().create_future()
        self._outcomes.put_nowait(("input", params))
        try:
            return await self._answer
        finally:
            self._answer = None

    async def _run(self, arguments: dict) -> None:
        cfg = self.upstream.cfg
        try:
            async with streamable_http_client(cfg.url, http_client=self.upstream.call_http) as (read, write, _):
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
