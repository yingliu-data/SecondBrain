"""Gateway settings (env) and `gateway.json` (M4-CONTRACTS.md §1, §6)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from protocol.mcp import NAMESPACE_SEP, valid_upstream_name
from pydantic import BaseModel, field_validator

logger = logging.getLogger("gateway")

DEFAULT_MAX_CONCURRENT = 2
DEFAULT_INPUT_EXPIRES_S = 3600
DEFAULT_META_TIMEOUT_S = 300


class UpstreamConfig(BaseModel):
    """One upstream MCP server. Same fields as agent-api's `mcp_servers` entries."""
    name: str
    url: str
    auth_token: str | None = None
    timeout_s: int = 300           # per-tool-call ceiling
    tool_defs_ttl_s: int = 300     # cached tools/list refresh interval
    allow_private: bool = False    # permit loopback/private-range URL (e.g. docker host)

    @field_validator("name")
    @classmethod
    def _no_namespace_separator(cls, v: str) -> str:
        if not valid_upstream_name(v):
            raise ValueError(f"upstream name must be non-empty and must not contain '{NAMESPACE_SEP}' or ':'")
        return v


class TenantPolicy(BaseModel):
    upstreams: list[str] = []
    allow: list[str] = ["*"]
    deny: list[str] = []
    promoted: list[str] = []
    max_concurrent: int = DEFAULT_MAX_CONCURRENT


class GatewayConfig(BaseModel):
    upstreams: dict[str, UpstreamConfig] = {}
    tenants: dict[str, TenantPolicy] = {}
    # How long a held upstream call waits for the guest's sb/input_response.
    input_expires_s: int = DEFAULT_INPUT_EXPIRES_S

    @classmethod
    def from_dict(cls, data: dict) -> "GatewayConfig":
        data = dict(data)
        ups = {name: {**cfg, "name": name} for name, cfg in (data.pop("upstreams", None) or {}).items()}
        return cls(upstreams=ups, **{k: v for k, v in data.items() if not k.startswith("_")})

    @classmethod
    def load(cls, path: str | os.PathLike) -> "GatewayConfig":
        p = Path(path)
        if not p.exists():
            logger.warning("gateway config %s not found; no upstreams, every tenant default-denied", p)
            return cls()
        return cls.from_dict(json.loads(p.read_text()))


# Settings field -> environment variable.
_ENV = {
    "config_path": "GATEWAY_CONFIG",
    "state_dir": "GATEWAY_STATE_DIR",
    "agent_api_url": "AGENT_API_URL",
    "internal_token": "INTERNAL_TOKEN",
    "allowed_private_hosts": "MCP_ALLOWED_PRIVATE_HOSTS",   # comma-separated
}


@dataclass
class Settings:
    config_path: str = "/data/gateway.json"
    state_dir: str = "/data/gateway-state"
    agent_api_url: str = "http://agent-api:8000"
    internal_token: str = ""
    allowed_private_hosts: list[str] = field(default_factory=lambda: ["host.docker.internal"])

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs: dict = {f: os.environ[e] for f, e in _ENV.items() if e in os.environ}
        if "allowed_private_hosts" in kwargs:
            kwargs["allowed_private_hosts"] = [h for h in kwargs["allowed_private_hosts"].split(",") if h]
        return cls(**kwargs)
