"""The gateway's /internal/* routes as agent-api calls them (M4-CONTRACTS §2–4, §8).

  identity_headers(user, tenant, session)   X-SB-User / X-SB-Tenant / X-SB-Session
  auth_headers(token)                       Authorization: Bearer (none when empty)
  bearer_ok(header, token)                  an incoming Authorization matches (empty token = ok)
  fetch_session_tools(...)   GET  /internal/tools                -> hello entries
  aclose_clients()           close the shared clients (app shutdown)

One httpx.AsyncClient per base URL is shared by every call (per-request
timeouts). Every call raises GatewayError on a transport failure, a non-2xx
status or a malformed body; callers decide what an outage means.
"""
from __future__ import annotations

import asyncio
import hmac

import httpx

from protocol.mcp import SESSION_HEADER, TENANT_HEADER, USER_HEADER

from app.config import INTERNAL_TOKEN, MCP_GATEWAY_TOOLS_TIMEOUT_S, MCP_GATEWAY_URL


class GatewayError(RuntimeError):
    pass


def identity_headers(user: str, tenant: str, session: str) -> dict:
    return {USER_HEADER: user, TENANT_HEADER: tenant, SESSION_HEADER: session}


def auth_headers(token: str = INTERNAL_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def bearer_ok(header: str | None, token: str = INTERNAL_TOKEN) -> bool:
    """An incoming Authorization header carries `token` (constant time);
    an empty token means no check (dev only)."""
    if not token:
        return True
    return hmac.compare_digest((header or "").encode(), f"Bearer {token}".encode())


# base_url -> (the loop it was made on, client): a client is bound to its loop
_clients: dict[str, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def _client(base_url: str) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    held = _clients.get(base_url)
    if held is None or held[0] is not loop or held[1].is_closed:
        held = _clients[base_url] = (loop, httpx.AsyncClient(base_url=base_url))
    return held[1]


async def aclose_clients() -> None:
    loop = asyncio.get_running_loop()
    for base_url, (owner, client) in list(_clients.items()):
        del _clients[base_url]
        if owner is loop:
            await client.aclose()


async def _request(method: str, path: str, *, base_url: str, headers: dict, timeout_s: float,
                   json_body=None):
    if not base_url:
        raise GatewayError("MCP_GATEWAY_URL is not set")
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0))
    try:
        resp = await _client(base_url).request(method, path, headers=headers, json=json_body,
                                               timeout=timeout)
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
