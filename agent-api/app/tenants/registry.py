import hmac
import json
import logging
from pathlib import Path

from app.config import API_SECRET_KEY, TENANTS_FILE
from .models import Tenant

logger = logging.getLogger("tenants")

GUEST_TENANT = "guest"


class TenantRegistry:
    """Loads tenants from a JSON config file.

    A synthetic "default" tenant keyed by the env API_SECRET_KEY always
    exists (unrestricted local skills, global prompt), so existing clients
    keep working even with no tenants file at all. The guest avatar profile
    (`guest`, auth origin, app/auth/guest.py) is synthesized too unless the
    file has an `auth: "origin"` entry named guest; it is reachable by name
    (routes/guest.py, the Temporal activities), never by an API key.
    The top-level `mcp_servers` map is the gateway's and is ignored here.
    Config is read once at startup; restart to apply changes.
    """

    def __init__(self, path: str | Path, default_api_key: str):
        self._tenants: list[Tenant] = []
        self._load(Path(path))

        if not any(t.api_key == default_api_key for t in self._tenants):
            self._tenants.append(Tenant(
                name="default", api_key=default_api_key, is_default=True,
            ))
        guest = next((t for t in self._tenants
                      if t.name == GUEST_TENANT and t.auth == "origin"), None)
        if guest is None:
            from app.auth.guest import guest_profile
            guest = guest_profile()
        self._guest = guest
        logger.info(f"Tenants loaded: {[t.name for t in self._tenants]}")

    def _load(self, path: Path):
        if not path.exists():
            logger.info(f"No tenants file at {path}; default tenant only")
            return
        data = json.loads(path.read_text())

        names, keys = set(), set()
        for entry in data.get("tenants", []):
            tenant = Tenant(**entry)
            if tenant.name in names:
                raise ValueError(f"Duplicate tenant name: {tenant.name}")
            if tenant.auth == "key":
                if tenant.api_key in keys:
                    raise ValueError(f"Duplicate api_key (tenant {tenant.name})")
                keys.add(tenant.api_key)
            names.add(tenant.name)
            self._tenants.append(tenant)

    # ── Public API ────────────────────────────────────────────

    def get_by_api_key(self, key: str) -> Tenant | None:
        """Constant-time scan; tenant count is tiny."""
        found = None
        for t in self._tenants:
            if t.auth == "key" and hmac.compare_digest(t.api_key, key):
                found = t
        return found

    def all_tenants(self) -> list[Tenant]:
        return list(self._tenants)

    def get_by_name(self, name: str) -> Tenant | None:
        found = next((t for t in self._tenants if t.name == name), None)
        if found is None and name == self._guest.name:
            return self._guest
        return found

    def guest(self) -> Tenant:
        """The guest avatar profile (auth origin)."""
        return self._guest

    def tenants_doc(self) -> dict:
        """{"tenants": [...]} in the shape flows.schedules.ensure_schedules
        reads (Tenant.workflow_entry: no api keys)."""
        return {"tenants": [t.workflow_entry() for t in self._tenants]}

    def all_origins(self) -> list[str]:
        """Every key tenant's registered origins (CORS; the guest profile's
        origins are added by main.py)."""
        origins = []
        for t in self._tenants:
            if t.auth == "key":
                origins.extend(t.origins)
        return sorted(set(origins))


def create_tenant_registry() -> TenantRegistry:
    return TenantRegistry(TENANTS_FILE, API_SECRET_KEY)
