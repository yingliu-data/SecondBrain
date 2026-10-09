"""Print a `gateway.json` from agent-api's `tenants.json` (M4-CONTRACTS.md §6).

    python -m app.migrate_config tenants.json > gateway.json

- `mcp_servers` → `upstreams` (same keys and fields).
- each tenant with `mcp_servers` → `{upstreams, allow: ["*"], deny: [],
  promoted (optional tenant `promoted` key), max_concurrent: 2}`.
- the default tenant (`is_default: true`, an entry whose api_key equals the
  env API_SECRET_KEY, or the synthesized `default` when neither exists) is
  unrestricted in agent-api, so it gets every upstream in the file.
"""
from __future__ import annotations

import json
import os
import sys

from app.config import TenantPolicy

_UPSTREAM_KEYS = ("url", "auth_token", "timeout_s", "tool_defs_ttl_s", "allow_private")


def _tenant(upstreams: list[str], promoted: list[str] | None = None) -> dict:
    return TenantPolicy(upstreams=upstreams, promoted=promoted or []).model_dump()


def migrate(tenants_file: dict, api_secret_key: str | None = None) -> dict:
    servers = tenants_file.get("mcp_servers") or {}
    upstreams = {name: {k: cfg[k] for k in _UPSTREAM_KEYS if k in cfg} for name, cfg in servers.items()}
    all_upstreams = list(upstreams)

    tenants: dict[str, dict] = {}
    has_default = False
    for entry in tenants_file.get("tenants") or []:
        is_default = bool(entry.get("is_default")) or (
            bool(api_secret_key) and entry.get("api_key") == api_secret_key)
        has_default |= is_default
        ups = all_upstreams if is_default else list(entry.get("mcp_servers") or [])
        if not ups:
            continue
        tenants[entry["name"]] = _tenant(ups, list(entry.get("promoted") or []))
    if not has_default and all_upstreams:
        tenants["default"] = _tenant(all_upstreams)
    return {"upstreams": upstreams, "tenants": tenants}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m app.migrate_config tenants.json > gateway.json", file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        data = json.load(f)
    json.dump(migrate(data, os.environ.get("API_SECRET_KEY")), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
