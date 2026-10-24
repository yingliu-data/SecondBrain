# code-index-mcp

An MCP server that turns a Python repo into a code graph and a name search, after
LocAgent, so the agent finds code without reading every file. It runs on the host,
like the wcc servers, and mcp-gateway reaches it as an upstream. It is a standalone
uv project with its own `uv.lock`, not a member of the root workspace.

Tools: `build_index(repo_path)`, `search_entities(repo_path, query, entity_type, detail)`,
`traverse_graph(repo_path, entity_id, edge_types)`, `get_entity_code(repo_path, entity_id)`.
`repo_path` is a path on the host. The index is saved per repo under
`$CODE_INDEX_HOME` (default `~/.cache/secondbrain/code-index`) and stays as built
until `build_index` runs again.

## Run

```bash
cd code-index-mcp && uv sync
CODE_INDEX_TOKEN=<token> uv run python -m code_index.server --host 0.0.0.0 --port 8770
```

The gateway container reaches the host only on a non-loopback address, hence
`--host 0.0.0.0` (the default is `127.0.0.1`). The server reads any `.py` file the
host user can read, so on any host but loopback it does not start without
`CODE_INDEX_TOKEN`; on loopback without it, requests are not checked.

## Gateway entry

Add this upstream to `mcp-gateway/data/gateway.json`, with `auth_token` equal to
`CODE_INDEX_TOKEN`, add `"code-index"` to the tenant's `upstreams`, and restart
mcp-gateway (`docker compose restart mcp-gateway`). It is also in
[gateway.json.example](../mcp-gateway/gateway.json.example).

```json
"code-index": {
  "url": "http://host.docker.internal:8770/mcp",
  "auth_token": "<must match CODE_INDEX_TOKEN on the code-index server>",
  "timeout_s": 600,
  "allow_private": true
}
```

The gateway names the tools `code-index__<tool>`, and the agent calls them through
`call_tool` (for example `code-index__build_index`).

## Test

```bash
cd code-index-mcp && uv run pytest -q
```

CI does not run these tests; its lint job (`uvx ruff@0.14.0 check .`) covers the folder.
