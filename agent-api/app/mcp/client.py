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

from app.tenants import MCPServerConfig

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


async def call_tool(cfg: MCPServerConfig, tool_name: str, arguments: dict) -> str:
    """Call one remote tool; flatten the result to text. Never raises for
    tool-level failures — returns 'Error: ...' strings the agent loop expects."""
    try:
        async with streamablehttp_client(
            cfg.url, headers=_headers(cfg),
            timeout=timedelta(seconds=30),
            sse_read_timeout=timedelta(seconds=cfg.timeout_s),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments),
                    timeout=cfg.timeout_s,
                )
    except asyncio.TimeoutError:
        return f"Error: tool '{tool_name}' on MCP server '{cfg.name}' timed out after {cfg.timeout_s}s."
    except Exception as e:
        reason = _unwrap(e)
        logger.error(f"MCP call failed [{cfg.name}/{tool_name}]: {reason}")
        return f"Error: MCP server '{cfg.name}' unreachable or failed: {reason}"

    texts = [c.text for c in result.content if getattr(c, "text", None)]
    text = "\n".join(texts) if texts else "(no text content returned)"
    if getattr(result, "isError", False):
        return f"Error from '{tool_name}': {text}"
    return text
