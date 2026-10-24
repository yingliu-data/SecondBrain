# index-mcp

An MCP server over a MongoDB Atlas store of code entities, after LocAgent, so the
agent finds code without reading every file. The indexer writes the store; the server
only reads it. It runs on the host, like the wcc servers, and mcp-gateway reaches it
as an upstream. It is a standalone uv project with its own `uv.lock`, not a member of
the root workspace.

## The store

One database (`MONGODB_DB`, default `secondbrain`) with four collections, shared by
every source so Markdown notes can join later: `files` (one manifest row per indexed
file: hash, size, mtime, versions, `removed_at`, `skipped_reason`), `nodes` (one
document per version of a directory, file, class or function), `edges` (`contains`,
`imports`, `invokes`, `inherits`, and `supersedes` between versions) and `chunks` (the
text Atlas embeds, one per class or function version). IDs are `code/<path>` for a
file and `code/<path>:<qualified name>` for an entity; a version is `<id>@v<n>`.

Indexing is add-only: a changed function gets a new version and a `supersedes` edge,
a removed file or function gets `removed_at`, a restored file continues its versions,
and nothing is deleted. A file node's text is its module-level code; the text of a
class or function is its own.

## Credentials

Four variables, in `.env` at the repository root (see `.env.example`) or in the
environment: `MONGODB_USERNAME`, `MONGODB_PASSWORD`, `MONGODB_HOST` (the cluster host
alone, the URI is built from it) and `MONGODB_DB`. The indexer needs read and write;
give the server a read-only Atlas user (Database Access, role `read` on the database)
in the environment it starts from, since the same variables serve both.

## Index a folder

```bash
cd index-mcp && uv sync
uv run python -m index_mcp.indexer sync /path/to/repo --source code
```

It creates the ordinary indexes and the three Atlas Search indexes of [atlas/](atlas/)
(the vector index embeds every chunk with `voyage-4`, which Atlas bills per token),
walks every `.py` file under the folder (not hidden folders, virtualenvs or installed
packages), and prints the counts. Run it again after the code changes: an unchanged
folder inserts nothing. A file that does not parse is recorded with its reason and
tried again next time.

## Run the server

```bash
INDEX_MCP_TOKEN=<token> uv run python -m index_mcp.server --host 0.0.0.0 --port 8770
```

The gateway container reaches the host only on a non-loopback address, hence
`--host 0.0.0.0` (the default is `127.0.0.1`). The server serves the indexed source
code, so on any host but loopback it does not start without `INDEX_MCP_TOKEN`; on
loopback without it, requests are not checked.

Tools, with one output shape for every entity (ID, type, location, status `latest`,
`superseded` or `removed`, and a preview or the text):

- `SearchEntity(query, scope, node_types, path_prefix, time_range, history, detail, limit)`:
  four layers, exact ID, name, keyword over names (Atlas Search), then content by
  keyword and meaning (vector search over the embedded chunks); the first layer with
  hits answers. Latest versions only unless `history`. If vector search fails, the
  content layer answers by keyword and logs a warning.
- `TraverseGraph(start_ids, direction, hops, node_types, edge_types, max_fanout)`:
  the entities within `hops` edges, one indexed query per hop.
- `RetrieveEntity(ids, detail, context_lines, page)`: the text with its line range,
  200 lines a page; a superseded version names the newest one.
- `SearchByTime(time_range, scope, node_types, limit)`: what was added, changed or
  removed in the range.

When MongoDB is down or the credentials are wrong, each tool returns an error that
says so and the server stays up.

## Gateway entry

Add this upstream to `mcp-gateway/data/gateway.json`, with `auth_token` equal to
`INDEX_MCP_TOKEN`, add `"index-mcp"` to the tenant's `upstreams`, and restart
mcp-gateway (`docker compose restart mcp-gateway`). It is also in
[gateway.json.example](../mcp-gateway/gateway.json.example).

```json
"index-mcp": {
  "url": "http://host.docker.internal:8770/mcp",
  "auth_token": "<must match INDEX_MCP_TOKEN on the index-mcp server>",
  "timeout_s": 600,
  "allow_private": true
}
```

The gateway names the tools `index-mcp__<tool>`, and the agent calls them through
`call_tool` (for example `index-mcp__SearchEntity`).

## Test

```bash
cd index-mcp && uv run pytest -q                              # offline, on mongomock
INDEX_MCP_LIVE=1 uv run pytest -q tests/test_live_atlas.py    # against Atlas, in a throw-away database
```

CI does not run these tests; its lint job (`uvx ruff@0.14.0 check .`) covers the folder.
