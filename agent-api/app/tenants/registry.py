import hmac
import json
import logging
from pathlib import Path

from app.config import API_SECRET_KEY, TENANTS_FILE
from .models import MCPServerConfig, Tenant

logger = logging.getLogger("tenants")


class TenantRegistry:
    """Loads tenants + MCP server definitions from a JSON config file.

    A synthetic "default" tenant keyed by the env API_SECRET_KEY always
    exists (unrestricted local skills, global prompt, no MCP servers), so
    existing clients keep working even with no tenants file at all.
    Config is read once at startup; restart to apply changes.
    """

    def __init__(self, path: str | Path, default_api_key: str):
        self._tenants: list[Tenant] = []
        self._mcp_servers: dict[str, MCPServerConfig] = {}
        self._load(Path(path))

        if not any(t.api_key == default_api_key for t in self._tenants):
            self._tenants.append(Tenant(
                name="default", api_key=default_api_key, is_default=True,
            ))
        logger.info(f"Tenants loaded: {[t.name for t in self._tenants]}; "
                    f"MCP servers: {list(self._mcp_servers)}")

    def _load(self, path: Path):
        if not path.exists():
            logger.info(f"No tenants file at {path}; default tenant only")
            return
        data = json.loads(path.read_text())

        for name, cfg in data.get("mcp_servers", {}).items():
            self._mcp_servers[name] = MCPServerConfig(name=name, **cfg)

        names, keys = set(), set()
        for entry in data.get("tenants", []):
            tenant = Tenant(**entry)
            if tenant.name in names:
                raise ValueError(f"Duplicate tenant name: {tenant.name}")
            if tenant.api_key in keys:
                raise ValueError(f"Duplicate api_key (tenant {tenant.name})")
            for server in tenant.mcp_servers:
                if server not in self._mcp_servers:
                    raise ValueError(
                        f"Tenant '{tenant.name}' references unknown MCP server '{server}'")
            names.add(tenant.name)
            keys.add(tenant.api_key)
            self._tenants.append(tenant)

    # ── Public API ────────────────────────────────────────────

    def get_by_api_key(self, key: str) -> Tenant | None:
        """Constant-time scan; tenant count is tiny."""
        found = None
        for t in self._tenants:
            if hmac.compare_digest(t.api_key, key):
                found = t
        return found

    def all_tenants(self) -> list[Tenant]:
        return list(self._tenants)

    def all_origins(self) -> list[str]:
        origins = []
        for t in self._tenants:
            origins.extend(t.origins)
        return sorted(set(origins))

    def mcp_server_configs(self) -> dict[str, MCPServerConfig]:
        return dict(self._mcp_servers)


def create_tenant_registry() -> TenantRegistry:
    return TenantRegistry(TENANTS_FILE, API_SECRET_KEY)
