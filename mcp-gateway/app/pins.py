"""Per-session pin tables (M4-CONTRACTS.md §5).

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
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from protocol.canonical import canonical_json, sha256_hex


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
        return {"user": self.user, "tenant": self.tenant, "session_id": self.session}


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
            "tools": self.tools,
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

    def get(self, ident: Identity) -> SessionPins | None:
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
        p = self.path(sp.ident)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(sp.to_json(), f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        dfd = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
