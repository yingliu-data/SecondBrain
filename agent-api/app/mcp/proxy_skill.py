"""MCPProxySkill — presents one remote MCP server as a local skill.

Tool definitions are fetched lazily/in the background and cached, so a down
MCP server never blocks boot; it just contributes zero tools until it comes
up. Tools are namespaced '{server}__{tool}' to avoid collisions.
"""
import asyncio
import logging
import time

from app.skills.base import BaseSkill
from app.tenants import MCPServerConfig
from . import client, ssrf
from .schema import mcp_tool_to_openai, split_namespaced

logger = logging.getLogger("mcp")

_RETRY_DELAYS = [10, 30]  # then every 60s


class MCPProxySkill(BaseSkill):

    def __init__(self, cfg: MCPServerConfig):
        self._cfg = cfg
        self._cached_defs: list[dict] = []
        self._fetched_at: float = 0.0
        self._refreshing = False

    # ── BaseSkill metadata ────────────────────────────────────

    @property
    def name(self) -> str:
        return f"mcp_{self._cfg.name}"

    @property
    def display_name(self) -> str:
        return f"MCP: {self._cfg.name}"

    @property
    def description(self) -> str:
        return f"Remote MCP server '{self._cfg.name}' ({len(self._cached_defs)} tools)"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def execution_side(self) -> str:
        return "server"

    @property
    def always_available(self) -> bool:
        # The keyword pre-filter can't know remote tool semantics; tenant
        # allowed_skills is the real gate.
        return True

    # ── Tool definitions (cached, never blocking) ─────────────

    def get_tool_definitions(self) -> list[dict]:
        if self._fetched_at and self._stale():
            self._kick_refresh()
        return self._cached_defs

    def _stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self._cfg.tool_defs_ttl_s

    def _kick_refresh(self):
        if not self._refreshing:
            try:
                asyncio.get_running_loop().create_task(self.refresh())
            except RuntimeError:
                pass  # no running loop (e.g. sync test context)

    async def refresh(self) -> bool:
        """Fetch tools/list and update the cache. Returns success."""
        if self._refreshing:
            return False
        self._refreshing = True
        try:
            ssrf.validate_mcp_url(self._cfg)
            tools = await client.fetch_tool_defs(self._cfg)
            self._cached_defs = [mcp_tool_to_openai(self._cfg.name, t) for t in tools]
            self._fetched_at = time.monotonic()
            logger.info(f"MCP '{self._cfg.name}': {len(self._cached_defs)} tools cached")
            return True
        except Exception as e:
            logger.warning(f"MCP '{self._cfg.name}': tools/list failed: {e}")
            return False
        finally:
            self._refreshing = False

    async def start_background_refresh(self):
        """Retry tools/list until the first success (10s, 30s, then 60s)."""
        attempt = 0
        while not self._cached_defs:
            if await self.refresh():
                return
            delay = _RETRY_DELAYS[attempt] if attempt < len(_RETRY_DELAYS) else 60
            attempt += 1
            await asyncio.sleep(delay)

    # ── Execution ─────────────────────────────────────────────

    async def execute(self, tool_name: str, arguments: dict) -> str:
        server, remote_tool = split_namespaced(tool_name)
        if server != self._cfg.name or not remote_tool:
            return f"Error: tool '{tool_name}' does not belong to MCP server '{self._cfg.name}'."
        try:
            ssrf.validate_mcp_url(self._cfg)  # re-check: DNS rebinding hedge
        except ValueError as e:
            return f"Error: {e}"
        return await client.call_tool(self._cfg, remote_tool, arguments)
