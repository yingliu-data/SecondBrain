"""Per-tenant tool policy.

Globs (fnmatch, case-sensitive) match the namespaced `{upstream}__{tool}`
name. Deny wins over allow. A tenant absent from `tenants`, or an upstream not
in the tenant's `upstreams` (or not configured at all) → no tools.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

from protocol.mcp import split_namespaced

from app.config import DEFAULT_MAX_CONCURRENT, GatewayConfig, TenantPolicy


class Policy:
    def __init__(self, config: GatewayConfig):
        self._config = config
        # The tenant's upstreams that are actually configured, in order.
        self._upstreams = {t: [u for u in dict.fromkeys(tp.upstreams) if u in config.upstreams]
                           for t, tp in config.tenants.items()}
        self._upstream_sets = {t: frozenset(ups) for t, ups in self._upstreams.items()}

    def tenant(self, tenant: str) -> TenantPolicy | None:
        return self._config.tenants.get(tenant)

    def upstreams_for(self, tenant: str) -> list[str]:
        return self._upstreams.get(tenant, [])

    def allowed(self, tenant: str, name: str) -> bool:
        tp = self.tenant(tenant)
        if tp is None:
            return False
        upstream, tool = split_namespaced(name)
        if not tool or upstream not in self._upstream_sets[tenant]:
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
    slot and re-takes it on resume without a limit check, so an
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
