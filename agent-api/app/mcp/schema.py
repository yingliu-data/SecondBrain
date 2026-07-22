"""Convert between MCP tool schemas and the OpenAI tool-definition format
used by the skill registry / vLLM."""

NAMESPACE_SEP = "__"


def namespaced(server_name: str, tool_name: str) -> str:
    return f"{server_name}{NAMESPACE_SEP}{tool_name}"


def split_namespaced(tool_name: str) -> tuple[str, str]:
    """'wcc__draft_event' -> ('wcc', 'draft_event'). Tool names may contain
    '__' themselves, so split on the FIRST separator only (server names are
    validated to never contain '__')."""
    server, _, tool = tool_name.partition(NAMESPACE_SEP)
    return server, tool


def mcp_tool_to_openai(server_name: str, tool) -> dict:
    """tool: mcp.types.Tool. MCP inputSchema is already JSON Schema, which is
    exactly what OpenAI-format 'parameters' expects."""
    parameters = tool.inputSchema or {"type": "object", "properties": {}}
    parameters.pop("$schema", None)
    return {
        "type": "function",
        "function": {
            "name": namespaced(server_name, tool.name),
            "description": f"[via {server_name} MCP] {tool.description or ''}".strip(),
            "parameters": parameters,
        },
    }
