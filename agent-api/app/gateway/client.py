"""The gateway's /internal/* routes as agent-api calls them (M4-CONTRACTS §2–4, §8).

  identity_headers(user, tenant, session)   X-SB-User / X-SB-Tenant / X-SB-Session
  auth_headers(token)                       Authorization: Bearer (none when empty)
  fetch_session_tools(...)   GET  /internal/tools                -> hello entries
  fetch_upstream_tools(...)  GET  /internal/upstreams/{n}/tools  -> OpenAI defs (session-less)
  call_tool(...)             POST /internal/call                 -> {content, is_error}

Every call raises GatewayError on a transport failure, a non-2xx status or a
malformed body; callers decide what an outage means.
"""
from __future__ import annotations

import httpx

from app.config import (INTERNAL_TOKEN, MCP_GATEWAY_CALL_TIMEOUT_S, MCP_GATEWAY_TOOLS_TIMEOUT_S,
                        MCP_GATEWAY_URL)

NAMESPACE_SEP = "__"


class GatewayError(RuntimeError):
    pass


def split_namespaced(tool_name: str) -> tuple[str, str]:
    """'wcc__draft_event' -> ('wcc', 'draft_event'); split on the FIRST
    separator (upstream names never contain '__')."""
    server, _, tool = tool_name.partition(NAMESPACE_SEP)
    return server, tool


def identity_headers(user: str, tenant: str, session: str) -> dict:
    return {"X-SB-User": user, "X-SB-Tenant": tenant, "X-SB-Session": session}


def auth_headers(token: str = INTERNAL_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _request(method: str, path: str, *, base_url: str, headers: dict, timeout_s: float,
                   json_body=None):
    if not base_url:
        raise GatewayError("MCP_GATEWAY_URL is not set")
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0))
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            resp = await client.request(method, path, headers=headers, json=json_body)
    except httpx.HTTPError as e:
        raise GatewayError(f"gateway unreachable: {type(e).__name__}: {e}") from e
    if resp.status_code // 100 != 2:
        raise GatewayError(f"gateway {method} {path}: HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise GatewayError(f"gateway {method} {path}: body is not JSON") from e


def _tool_list(data, path: str) -> list[dict]:
    tools = data.get("tools") if isinstance(data, dict) else data
    if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
        raise GatewayError(f"gateway {path}: no tool list in the answer")
    return tools


async def fetch_session_tools(user: str, tenant: str, session: str, *,
                              base_url: str = MCP_GATEWAY_URL, token: str = INTERNAL_TOKEN,
                              timeout_s: float = MCP_GATEWAY_TOOLS_TIMEOUT_S) -> list[dict]:
    """The session's pinned tool surface, hello-entry shaped (§4), verbatim."""
    path = "/internal/tools"
    data = await _request("GET", path, base_url=base_url, timeout_s=timeout_s,
                          headers={**auth_headers(token), **identity_headers(user, tenant, session)})
    return _tool_list(data, path)


async def fetch_upstream_tools(name: str, *, base_url: str = MCP_GATEWAY_URL,
                               token: str = INTERNAL_TOKEN,
                               timeout_s: float = 30.0) -> list[dict]:
    """One upstream's tools as OpenAI function defs (session-less: no identity)."""
    path = f"/internal/upstreams/{name}/tools"
    data = await _request("GET", path, base_url=base_url, timeout_s=timeout_s,
                          headers=auth_headers(token))
    return _tool_list(data, path)


async def call_tool(name: str, arguments: dict, user: str, tenant: str, session: str, *,
                    base_url: str = MCP_GATEWAY_URL, token: str = INTERNAL_TOKEN,
                    timeout_s: float = MCP_GATEWAY_CALL_TIMEOUT_S) -> dict:
    data = await _request("POST", "/internal/call", base_url=base_url, timeout_s=timeout_s,
                          headers={**auth_headers(token), **identity_headers(user, tenant, session)},
                          json_body={"name": name, "arguments": arguments})
    if not isinstance(data, dict) or "content" not in data:
        raise GatewayError("gateway /internal/call: no content in the answer")
    return data
