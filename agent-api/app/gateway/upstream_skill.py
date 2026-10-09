"""GatewayUpstreamSkill — one MCP upstream as a legacy-engine skill, through
the gateway (M4-CONTRACTS §8; replaces MCPProxySkill).

Skill name `mcp_{upstream}` as before, so `Tenant.allowed_skill_names()` is
unchanged. Tool definitions (`{upstream}__{tool}`, OpenAI shape) come from
`GET /internal/upstreams/{name}/tools`, cached and refreshed in the
background, so a down gateway never blocks boot: the skill just has no tools
until it answers. `execute` posts `/internal/call` with the request's
identity (user, tenant, legacy session id from the skill context); the
gateway applies the tenant's policy, the upstream timeout and the SSRF rules.
"""
from __future__ import annotations

import asyncio
import logging
import time

from protocol.mcp import split_namespaced

from app.config import INTERNAL_TOKEN, MCP_GATEWAY_CALL_TIMEOUT_S, MCP_GATEWAY_URL
from app.gateway import client
from app.gateway.client import GatewayError
from app.skills.base import (BaseSkill, get_current_session_id, get_current_tenant,
                             get_current_user_id)

logger = logging.getLogger("gateway")

_RETRY_DELAYS = [10, 30]  # then every 60s


class GatewayUpstreamSkill(BaseSkill):
    # the harness guest calls the gateway itself over :5011, never as a host tool
    engines = frozenset({"legacy"})

    def __init__(self, upstream: str, *, base_url: str = MCP_GATEWAY_URL,
                 token: str = INTERNAL_TOKEN, tool_defs_ttl_s: float = 300,
                 call_timeout_s: float = MCP_GATEWAY_CALL_TIMEOUT_S):
        self.upstream = upstream
        self.base_url = base_url
        self.token = token
        self.tool_defs_ttl_s = tool_defs_ttl_s
        self.call_timeout_s = call_timeout_s
        self._cached_defs: list[dict] = []
        self._fetched_at: float = 0.0
        self._refreshing = False

    # ── BaseSkill metadata ────────────────────────────────────

    @property
    def name(self) -> str:
        return f"mcp_{self.upstream}"

    @property
    def display_name(self) -> str:
        return f"MCP: {self.upstream}"

    @property
    def description(self) -> str:
        return f"Remote MCP server '{self.upstream}' via the gateway ({len(self._cached_defs)} tools)"

    @property
    def version(self) -> str:
        return "2.0.0"

    @property
    def execution_side(self) -> str:
        return "server"

    @property
    def always_available(self) -> bool:
        # the keyword pre-filter cannot know remote tool semantics; the
        # tenant's allowed_skills (and the gateway policy) is the gate
        return True

    # ── Tool definitions (cached, never blocking) ─────────────

    def get_tool_definitions(self) -> list[dict]:
        if self._fetched_at and self._stale():
            self._kick_refresh()
        return self._cached_defs

    def _stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self.tool_defs_ttl_s

    def _kick_refresh(self) -> None:
        if not self._refreshing:
            try:
                asyncio.get_running_loop().create_task(self.refresh())
            except RuntimeError:
                pass  # no running loop (sync context)

    async def refresh(self) -> bool:
        """Fetch the upstream's tools from the gateway; True on success."""
        if self._refreshing:
            return False
        self._refreshing = True
        try:
            defs = await client.fetch_upstream_tools(self.upstream, base_url=self.base_url,
                                                     token=self.token)
            self._cached_defs = defs          # the route lists this upstream's only
            self._fetched_at = time.monotonic()
            logger.info("gateway upstream '%s': %d tools cached", self.upstream,
                        len(self._cached_defs))
            return True
        except GatewayError as e:
            logger.warning("gateway upstream '%s': tools failed: %s", self.upstream, e)
            return False
        finally:
            self._refreshing = False

    async def start_background_refresh(self) -> None:
        """Retry until the first success (10s, 30s, then every 60s)."""
        attempt = 0
        while not self._fetched_at:
            if await self.refresh():
                return
            delay = _RETRY_DELAYS[attempt] if attempt < len(_RETRY_DELAYS) else 60
            attempt += 1
            await asyncio.sleep(delay)

    # ── Execution ─────────────────────────────────────────────

    async def execute(self, tool_name: str, arguments: dict) -> str:
        upstream, remote_tool = split_namespaced(tool_name)
        if upstream != self.upstream or not remote_tool:
            return f"Error: tool '{tool_name}' does not belong to MCP server '{self.upstream}'."
        user, tenant, session = get_current_user_id(), get_current_tenant(), get_current_session_id()
        if not (user and tenant and session):
            return f"Error: tool '{tool_name}' needs a user, tenant and session to call the MCP gateway."
        try:
            result = await client.call_tool(tool_name, arguments, user, tenant, session,
                                            base_url=self.base_url, token=self.token,
                                            timeout_s=self.call_timeout_s)
        except GatewayError as e:
            logger.error("gateway call failed [%s]: %s", tool_name, e)
            return f"Error: MCP gateway unreachable or failed: {e}"
        text = str(result.get("content") or "(no text content returned)")
        # "Error" prefix: the legacy loop and sides.py read it as a failure
        return f"Error: {text}" if result.get("is_error") else text
