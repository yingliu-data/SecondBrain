"""Tool naming and format conversion (moved from agent-api app/mcp/schema.py).

Names everywhere are `{upstream}__{tool}`. `tool_defn` is the canonical MCP
definition a pin hashes (M4-CONTRACTS.md §5); `defn_to_openai` turns a
definition into the OpenAI function format used by hello entries and the
legacy engine.
"""
from __future__ import annotations

import copy

from mcp import types

NAMESPACE_SEP = "__"


def namespaced(server_name: str, tool_name: str) -> str:
    return f"{server_name}{NAMESPACE_SEP}{tool_name}"


def split_namespaced(tool_name: str) -> tuple[str, str]:
    """'wcc__draft_event' -> ('wcc', 'draft_event'). Tool names may contain
    '__' themselves, so split on the FIRST separator only (upstream names are
    validated to never contain '__')."""
    server, _, tool = tool_name.partition(NAMESPACE_SEP)
    return server, tool


def tool_defn(tool: types.Tool) -> dict:
    """The upstream MCP tool as `{name, description, inputSchema}` plus
    `outputSchema` / `annotations` when present; name un-namespaced."""
    # model_dump returns fresh dicts, so nothing downstream can mutate the
    # SDK object (and a def never hashes two ways).
    dumped = tool.model_dump(mode="json", exclude_none=True)
    defn = {k: dumped[k] for k in _DEFN_KEYS if k in dumped}
    defn.setdefault("inputSchema", {"type": "object", "properties": {}})
    return defn


_DEFN_KEYS = ("name", "description", "inputSchema", "outputSchema", "annotations")


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


def mcp_tool_to_openai(server_name: str, tool: types.Tool) -> dict:
    """What MCPProxySkill returned for one upstream tool."""
    return defn_to_openai(namespaced(server_name, tool.name), tool_defn(tool), via=server_name)
