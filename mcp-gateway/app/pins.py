"""Per-session pin tables.

`{state_dir}/pins/{user}/{tenant}/{session}.json`:

    {"user", "tenant", "session", "created_at",
     "tools": {"<upstream>__<tool>": {"pin": "<64 hex>", "defn": {...}}},
     "quarantined": ["<upstream>__<tool>", ...]}

Written atomically (temp file + fsync + rename) when the table is created and
whenever a tool is quarantined, and read back after a restart.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from protocol.canonical import canonical_json, sha256_hex
from protocol.fsio import atomic_write
from protocol.mcp import gateway_event_identity


def pin_of(defn: dict) -> str:
    return sha256_hex(canonical_json(defn))


@dataclass(frozen=True)
class Identity:
    user: str
    tenant: str
    session: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.user, self.tenant, self.session)

    def event_fields(self) -> dict:
        return gateway_event_identity(self.user, self.tenant, self.session)


@dataclass
class SessionPins:
    ident: Identity
    tools: dict[str, dict] = field(default_factory=dict)   # name -> {"pin", "defn"}
    quarantined: set[str] = field(default_factory=set)
    # Upstreams the tenant was allowed but that could not be listed at pin
    # time; their tools are pinned the first time they answer (no existing
    # pin ever changes).
    unlisted: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_json(self) -> dict:
        return {
            "user": self.ident.user, "tenant": self.ident.tenant, "session": self.ident.session,
            "created_at": self.created_at,
            "tools": dict(self.tools),   # a snapshot: may be written from a worker thread
            "quarantined": sorted(self.quarantined),
            "unlisted": self.unlisted,
        }

    @classmethod
    def from_json(cls, ident: Identity, data: dict) -> "SessionPins":
        return cls(ident=ident, tools=dict(data.get("tools") or {}),
                   quarantined=set(data.get("quarantined") or []),
                   unlisted=list(data.get("unlisted") or []),
                   created_at=float(data.get("created_at") or 0.0))


class PinStore:
    def __init__(self, state_dir: str | os.PathLike):
        self._root = Path(state_dir) / "pins"
        self._cache: dict[tuple, SessionPins] = {}

    def path(self, ident: Identity) -> Path:
        return self._root / ident.user / ident.tenant / f"{ident.session}.json"

    def cached(self, ident: Identity) -> SessionPins | None:
        return self._cache.get(ident.key)

    def get(self, ident: Identity) -> SessionPins | None:
        """Memory first, then disk (blocking I/O)."""
        sp = self._cache.get(ident.key)
        if sp is not None:
            return sp
        p = self.path(ident)
        if not p.exists():
            return None
        sp = SessionPins.from_json(ident, json.loads(p.read_text()))
        self._cache[ident.key] = sp
        return sp

    def create(self, ident: Identity, tools: dict[str, dict], unlisted: list[str]) -> SessionPins:
        sp = SessionPins(ident=ident, tools=tools, unlisted=unlisted, created_at=time.time())
        self.save(sp)
        self._cache[ident.key] = sp
        return sp

    def save(self, sp: SessionPins) -> None:
        self.write(sp.ident, sp.to_json())

    def write(self, ident: Identity, data: dict) -> None:
        """Atomic write of a `SessionPins.to_json()` snapshot (blocking I/O)."""
        p = self.path(ident)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, json.dumps(data, sort_keys=True).encode())     # durable
