"""Tool definition and format conversion (moved from agent-api app/mcp/schema.py).

Names everywhere are `{upstream}__{tool}` (`protocol.mcp.namespaced`).
`tool_defn` is the canonical MCP definition a pin hashes (M4-CONTRACTS.md §5);
`defn_to_openai` turns a definition into the OpenAI function format used by
hello entries and the legacy engine.
"""
from __future__ import annotations

import copy

from mcp import types

_DEFN_KEYS = ("name", "description", "inputSchema", "outputSchema", "annotations")


def tool_defn(tool: types.Tool) -> dict:
    """The upstream MCP tool as `{name, description, inputSchema}` plus
    `outputSchema` / `annotations` when present; name un-namespaced."""
    # model_dump returns fresh dicts, so nothing downstream can mutate the
    # SDK object (and a def never hashes two ways).
    dumped = tool.model_dump(mode="json", exclude_none=True)
    defn = {k: dumped[k] for k in _DEFN_KEYS if k in dumped}
    defn.setdefault("inputSchema", {"type": "object", "properties": {}})
    return defn


def defn_to_openai(name: str, defn: dict, via: str | None = None) -> dict:
    """OpenAI function definition for a tool definition. `name` is the name
    the model sees (namespaced for upstream tools); `via` adds the
    '[via X MCP]' prefix MCPProxySkill used."""
    parameters = copy.deepcopy(defn.get("inputSchema") or {"type": "object", "properties": {}})
    parameters.pop("$schema", None)
    description = defn.get("description") or ""
    if via:
        description = f"[via {via} MCP] {description}".strip()
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }
