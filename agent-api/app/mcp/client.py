"""Thin MCP client: short-lived streamable-HTTP session per operation.

Holding a ClientSession across requests fights the SDK's anyio cancel-scope
semantics; call volume is chat-paced and the tools themselves run for
seconds-to-minutes, so two extra round-trips per call are negligible.
"""
import asyncio
import logging
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import time

from app.tenants import MCPServerConfig
from sb_contracts.enums import ExecutionSide, TierStatus, ToolOutcome
from sb_contracts.interfaces import MCPClient
from sb_contracts.models import TierHealth, ToolCall, ToolDescriptor, ToolResult

logger = logging.getLogger("mcp")


def _unwrap(e: BaseException) -> str:
    """anyio task groups wrap the real failure in ExceptionGroups — surface
    the first leaf exception so error strings stay readable."""
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


def _headers(cfg: MCPServerConfig) -> dict | None:
    if cfg.auth_token:
        return {"Authorization": f"Bearer {cfg.auth_token}"}
    return None


async def fetch_tool_defs(cfg: MCPServerConfig) -> list:
    """Return the server's mcp.types.Tool list."""
    async with streamablehttp_client(
        cfg.url, headers=_headers(cfg),
        timeout=timedelta(seconds=30),
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def call_tool(cfg: MCPServerConfig, tool_name: str, arguments: dict,
                    timeout_s: int | None = None) -> str:
    """Call one remote tool; flatten the result to text. Never raises for
    tool-level failures — returns 'Error: ...' strings the agent loop expects.

    timeout_s overrides the server's configured ceiling for this one call. The
    MCPClient contract declares a per-call timeout, and silently substituting
    the config value would mean a caller asking for 10s on a voice turn quietly
    waiting out a 2700s pipeline ceiling."""
    limit = cfg.timeout_s if timeout_s is None else timeout_s
    try:
        async with streamablehttp_client(
            cfg.url, headers=_headers(cfg),
            timeout=timedelta(seconds=30),
            sse_read_timeout=timedelta(seconds=limit),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments),
                    timeout=limit,
                )
    except asyncio.TimeoutError:
        return f"Error: tool '{tool_name}' on MCP server '{cfg.name}' timed out after {limit}s."
    except Exception as e:
        reason = _unwrap(e)
        logger.error(f"MCP call failed [{cfg.name}/{tool_name}]: {reason}")
        return f"Error: MCP server '{cfg.name}' unreachable or failed: {reason}"

    texts = [c.text for c in result.content if getattr(c, "text", None)]
    text = "\n".join(texts) if texts else "(no text content returned)"
    if getattr(result, "isError", False):
        return f"Error from '{tool_name}': {text}"
    return text


class StreamableHTTPClient(MCPClient):
    """The module-level functions above, behind the shared MCPClient contract.

    A thin adapter rather than a rewrite: the transport semantics here (a
    short-lived session per operation, because holding a ClientSession across
    requests fights the SDK's anyio cancel-scope handling) are load-bearing and
    were arrived at the hard way. This exists so the interface is exercised
    against real code, and so a FakeMCPClient can stand in during tests.

    Note this class will move to mcp-gateway with Phase 1, when agent-api stops
    speaking MCP directly and starts speaking to the gateway instead.
    """

    def __init__(self, cfg: MCPServerConfig) -> None:
        self._cfg = cfg

    async def list_tools(self) -> list[ToolDescriptor]:
        tools = await fetch_tool_defs(self._cfg)
        return [
            ToolDescriptor(
                name=f"{self._cfg.name}__{t.name}",
                description=getattr(t, "description", "") or "",
                parameters=getattr(t, "inputSchema", {}) or {},
                side=ExecutionSide.SERVER,
                timeout_s=self._cfg.timeout_s,
            )
            for t in tools
        ]

    async def call_tool(self, call: ToolCall, *, timeout_s: int) -> ToolResult:
        started = time.monotonic()
        # _call_tool_raw, not call_tool: a plain call would resolve correctly
        # (class scope is skipped by LEGB inside a method body) but reads like
        # recursion. The alias says which one is meant.
        text = await _call_tool_raw(self._cfg, call.name, call.arguments,
                                    timeout_s=timeout_s)
        elapsed = int((time.monotonic() - started) * 1000)
        # call_tool() never raises: it encodes failure in the string, which is
        # what goes back to the model. Map that to a typed outcome here.
        if text.startswith("Error:") and "timed out" in text:
            outcome = ToolOutcome.TIMEOUT
        elif text.startswith("Error"):
            outcome = ToolOutcome.ERROR
        else:
            outcome = ToolOutcome.OK
        return ToolResult(call_id=call.id, outcome=outcome, content=text,
                          duration_ms=elapsed,
                          error=text if outcome is not ToolOutcome.OK else None)

    async def health(self) -> TierHealth:
        try:
            await fetch_tool_defs(self._cfg)
            return TierHealth(tier=f"mcp:{self._cfg.name}", status=TierStatus.OK)
        except Exception as e:
            return TierHealth(tier=f"mcp:{self._cfg.name}", status=TierStatus.DOWN,
                              detail=_unwrap(e))

    async def aclose(self) -> None:
        """No-op: sessions are per-operation, so there is nothing to hold open."""


# Alias for StreamableHTTPClient.call_tool, whose own name makes an
# unqualified reference read like recursion.
_call_tool_raw = call_tool
