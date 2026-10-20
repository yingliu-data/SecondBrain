import hmac
import json
import logging
from pathlib import Path

from app.config import API_SECRET_KEY, TENANTS_FILE
from .models import Tenant

logger = logging.getLogger("tenants")

GUEST_TENANT = "guest"
RETIRED_TENANTS = {"wcc-event", "wcc-mentorship", "wcc-analytic"}


class TenantRegistry:
    """Loads tenants from a JSON config file.

    A synthetic "default" tenant keyed by the env API_SECRET_KEY always
    exists (unrestricted local skills, global prompt), so existing clients
    keep working even with no tenants file at all. The guest avatar profile
    (`guest`, auth origin, app/auth/guest.py) is synthesized too unless the
    file has an `auth: "origin"` entry named guest; it is a tenant like the
    others (by name: routes/guest.py, the Temporal activities), never matched
    by an API key. `auth: "origin"` is for the guest only (no other route
    serves origin tenants). The top-level `mcp_servers` map is the
    gateway's and is ignored here. Config is read once at startup; restart
    to apply changes.
    """

    def __init__(self, path: str | Path, default_api_key: str):
        self._tenants: list[Tenant] = []
        self._load(Path(path))

        if not any(t.api_key == default_api_key for t in self._tenants):
            self._tenants.append(Tenant(
                name="default", api_key=default_api_key, is_default=True,
            ))
        if self.get_by_name(GUEST_TENANT) is None:
            from app.auth.guest import guest_profile
            self._tenants.append(guest_profile())
        logger.info(f"Tenants loaded: {[t.name for t in self._tenants]}")

    def _load(self, path: Path):
        if not path.exists():
            logger.info(f"No tenants file at {path}; default tenant only")
            return
        data = json.loads(path.read_text())

        names, keys = set(), set()
        for entry in data.get("tenants", []):
            if entry.get("name") in RETIRED_TENANTS:
                logger.warning(f"tenant {entry['name']!r} is retired (issue #49) and skipped: "
                               "its API key is refused. The default tenant has every MCP "
                               "server. Remove the entry from tenants.json.")
                continue
            tenant = Tenant(**entry)
            if (tenant.auth == "origin") != (tenant.name == GUEST_TENANT):
                raise ValueError(f"tenant {tenant.name!r}: auth: origin is for the "
                                 f"{GUEST_TENANT!r} tenant only (and it must use it)")
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
        return next((t for t in self._tenants if t.name == name), None)

    def guest(self) -> Tenant:
        """The guest avatar profile (auth origin)."""
        return self.get_by_name(GUEST_TENANT)

    def tenants_doc(self) -> dict:
        """{"tenants": [...]} in the shape flows.schedules.ensure_schedules
        reads (Tenant.workflow_entry: no api keys); a tenant without
        `wall_clock_s` gets HARNESS_WALL_CLOCK_S, as worker.session_config
        gives it on the chat path."""
        from app.config import HARNESS_WALL_CLOCK_S
        return {"tenants": [{**t.workflow_entry(),
                             "wall_clock_s": (HARNESS_WALL_CLOCK_S if t.wall_clock_s is None
                                              else t.wall_clock_s)}
                            for t in self._tenants]}

    def all_origins(self) -> list[str]:
        """Every tenant's registered origins, the guest's included (CORS)."""
        return sorted({o for t in self._tenants for o in t.origins})


def create_tenant_registry() -> TenantRegistry:
    return TenantRegistry(TENANTS_FILE, API_SECRET_KEY)
