"""Per-tenant tool policy (M4-CONTRACTS.md §6).

Globs (fnmatch, case-sensitive) match the namespaced `{upstream}__{tool}`
name. Deny wins over allow. A tenant absent from `tenants`, or an upstream not
in the tenant's `upstreams` (or not configured at all) → no tools.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

from app.config import DEFAULT_MAX_CONCURRENT, GatewayConfig, TenantPolicy
from app.schema import split_namespaced


class Policy:
    def __init__(self, config: GatewayConfig):
        self._config = config

    def tenant(self, tenant: str) -> TenantPolicy | None:
        return self._config.tenants.get(tenant)

    def upstreams_for(self, tenant: str) -> list[str]:
        """The tenant's upstreams that are actually configured, in order."""
        tp = self.tenant(tenant)
        if tp is None:
            return []
        return [u for u in dict.fromkeys(tp.upstreams) if u in self._config.upstreams]

    def allowed(self, tenant: str, name: str) -> bool:
        tp = self.tenant(tenant)
        if tp is None:
            return False
        upstream, tool = split_namespaced(name)
        if not tool or upstream not in self.upstreams_for(tenant):
            return False
        if any(fnmatchcase(name, g) for g in tp.deny):
            return False
        return any(fnmatchcase(name, g) for g in tp.allow)

    def promoted(self, tenant: str) -> list[str]:
        tp = self.tenant(tenant)
        if tp is None:
            return []
        return [n for n in dict.fromkeys(tp.promoted) if self.allowed(tenant, n)]

    def max_concurrent(self, tenant: str) -> int:
        tp = self.tenant(tenant)
        return tp.max_concurrent if tp is not None else DEFAULT_MAX_CONCURRENT


class ConcurrencyLimiter:
    """Running upstream calls per session. A call held for input releases its
    slot (§3.2) and re-takes it on resume without a limit check, so an
    answered question is never refused."""

    def __init__(self):
        self._running: dict[tuple, int] = {}

    def try_acquire(self, key: tuple, limit: int) -> bool:
        if self._running.get(key, 0) >= limit:
            return False
        self.force_acquire(key)
        return True

    def force_acquire(self, key: tuple) -> None:
        self._running[key] = self._running.get(key, 0) + 1

    def release(self, key: tuple) -> None:
        n = self._running.get(key, 0) - 1
        if n > 0:
            self._running[key] = n
        else:
            self._running.pop(key, None)

    def running(self, key: tuple) -> int:
        return self._running.get(key, 0)
