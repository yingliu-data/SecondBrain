# M4 contracts — the MCP gateway

Fixed before the M4 work split across three workstreams (gateway service,
guest, host). Each workstream owns its files; these contracts are the seams.
PLAN.md section 9 is the design; this file pins the wire details.

## 1. Pieces and paths

```text
guest (VM) ──HTTP over vsock :5011──▶ host forwarder ──HTTP──▶ mcp-gateway ──MCP──▶ upstreams
                                      (agent-api, per session;   (own container)
                                       stamps identity headers)
worker (agent-api) ──HTTP──▶ mcp-gateway /internal/*   (session-start tool list, legacy engine)
mcp-gateway ──HTTP──▶ agent-api POST /internal/gateway-events   (host log)
```

Env: agent-api `MCP_GATEWAY_URL` (default `http://mcp-gateway:8090`);
mcp-gateway `GATEWAY_CONFIG` (default `/data/gateway.json`), `GATEWAY_STATE_DIR`
(default `/data/gateway-state`), `AGENT_API_URL` (default `http://agent-api:8000`);
both `INTERNAL_TOKEN` (shared bearer for the `/internal/*` routes, both
directions; empty = no check, dev only).

## 2. Identity headers

Every request into the gateway except `/healthz` carries
`X-SB-User`, `X-SB-Tenant`, `X-SB-Session` (= the event-log `session_id`;
for the legacy engine, the legacy session id). The forwarder sets them and
**removes any `X-SB-*` header the guest sent**. Missing → 400. The assurance
behind them is OpenShell's (PLAN.md 11).

## 3. Gateway HTTP surface

| Route | Caller | Behaviour |
|---|---|---|
| `POST /mcp` | guest via forwarder | MCP streamable HTTP, **stateless, JSON responses** (`json_response=True`): no `initialize` needed, each POST is one JSON-RPC request → one JSON body. Methods: `tools/list`, `tools/call`. |
| `GET /internal/tools` | worker at session start | Policy-filtered surface for the tenant **in hello shape** (§4) and pins it for the session (first call pins; later calls return the same pins). |
| `GET /internal/upstreams/{name}/tools` | legacy engine | That upstream's full tool list as OpenAI function defs, names `{upstream}__{tool}` (what `MCPProxySkill` returned). No pinning. |
| `POST /internal/call` | legacy engine | `{name: "{upstream}__{tool}", arguments}` → `{content: str, is_error: bool}`; policy applies (tenant header). |
| `GET /healthz` | compose | `{ok: true}` |

`/internal/*` require `Authorization: Bearer $INTERNAL_TOKEN`.

### 3.1 The MCP surface (`/mcp`)

`tools/list` = the tenant's promoted tools + three meta tools:

| Tool | Arguments | Result text |
|---|---|---|
| `search_tools` | `query` str, `limit` int (default 10) | JSON list `[{name, description}]`, policy-filtered |
| `describe_tool` | `name` str | JSON `{name, description, inputSchema, pin}` (the pinned def) |
| `call_tool` | `name` str (`upstream__tool`), `arguments` object | the upstream result |

Promoted tools are callable directly by their `upstream__tool` name.
Tool names everywhere use the existing namespacing `{upstream}__{tool}`
(`app/mcp/schema.py` `split_namespaced`).

A `tools/call` result is the MCP `CallToolResult` (`content` list of text
items, `isError`). Gateway-side failures (policy deny, quarantine, unknown
tool, upstream timeout/unreachable, concurrency limit) are results with
`isError: true` and a clear text, never JSON-RPC errors (those are reserved for
malformed requests).

### 3.2 Input required (elicitation relay)

When an upstream asks for input mid-call (MCP elicitation), the gateway holds
the upstream call open and returns to the guest a result with
`isError: false`, `content: [{type: text, text: <message>}]` and

```json
"_meta": {"sb/input_required": {"request_state": "<opaque>", "message": "...",
                                 "schema": {...}, "expires_in_s": 3600}}
```

The guest turns it into a `question` (text = message; options from the schema
when it is a single enum, else free text), and on `ui_response` re-sends the
**same** `tools/call` with

```json
"params": {"name": ..., "arguments": ..., "_meta": {"sb/input_response":
   {"request_state": "<same>", "action": "accept" | "decline" | "cancel",
    "content": {...}}}}
```

The gateway feeds the answer to the held upstream callback and answers this
second POST with the final result (or another `sb/input_required`). Unknown or
expired `request_state` → `isError: true` "input request expired". A question
deadline that passes → the guest sends `action: "cancel"`. The held upstream
call lives in gateway memory (one replica; a gateway restart loses it → the
call's result is an error). All of it is one operation, one `call_id`.

## 4. Hello tool entries (worker → guest)

`GET /internal/tools` returns `{"tools": [entry...]}`; each entry goes into
`hello.config.tools` verbatim:

```json
{"name": "wcc-events__draft_event" | "search_tools" | "describe_tool" | "call_tool",
 "side": "mcp", "pin": "<64 hex>" ,
 "timeout_s": 2700,
 "description": "...",
 "schema": {"type": "function", "function": {"name", "description", "parameters"}}}
```

Meta tools get `pin` = sha256 of their own canonical definition and `timeout_s`
= the max over the tenant's upstreams. `session_start.tools` records name, side
and pin as today. The guest's op timeout for an mcp call is the entry's
`timeout_s` (fallback `limits.op_timeout_s.mcp`).

## 5. Pins and quarantine

`pin = protocol.canonical.sha256_hex(canonical_json(defn))` where `defn` is the
upstream MCP tool as `{name, description, inputSchema}` (+ `outputSchema`,
`annotations` when present), name un-namespaced. The pin table for a session
is written to `{GATEWAY_STATE_DIR}/pins/{user}/{tenant}/{session}.json` at the
first `/internal/tools` or `/mcp` call (atomic write) and read back after a
restart, so a gateway restart never loses a pin. A tool whose current upstream
definition hashes differently from the session's pin is **quarantined for
that session**: calls return `isError: true` "tool definition changed since
this session started (quarantined)". New sessions pin the new definition.
Definitions are cached per upstream for `tool_defs_ttl_s`.

## 6. Policy (`gateway.json`)

```json
{"upstreams": {"wcc-events": {"url", "auth_token", "timeout_s", "tool_defs_ttl_s", "allow_private"}},
 "tenants": {"wcc-event": {"upstreams": ["wcc-events"], "allow": ["*"], "deny": [],
                           "promoted": ["wcc-events__draft_event"], "max_concurrent": 2}}}
```

`upstreams` keys = today's `tenants.json` `mcp_servers` keys. Globs match the
namespaced name; deny wins over allow; a tenant absent from `tenants` or an
upstream not listed → no tools (default deny). `max_concurrent` per session
(default 2). `python -m app.migrate_config tenants.json` prints a
`gateway.json` from today's tenants file (each tenant's `mcp_servers` →
`upstreams`, `allow: ["*"]`, `promoted` from an optional tenant `promoted` key).

## 7. Gateway events

The gateway posts `POST {AGENT_API_URL}/internal/gateway-events` (bearer
`INTERNAL_TOKEN`) with

```json
{"user", "tenant", "session_id", "op": "list" | "call" | "quarantine" | "error",
 "tool"?: str, "upstream"?: str, "ok"?: bool, "ms"?: int, "error"?: str}
```

fire-and-forget (a failed post is logged, never fails the call). agent-api
appends it as a `gateway` host event to that session's host log when the
session directory exists (worker or not), else drops it with a log line.

## 8. Legacy engine

`MCPProxySkill` and `app/mcp/` are removed from agent-api. Their code moves to
`mcp-gateway/app/` (`client.py`, `schema.py`, `ssrf.py`). The legacy engine
gets `GatewayUpstreamSkill(name)` — one per upstream, skill name
`mcp_{upstream}` as before (so `Tenant.allowed_skill_names` is unchanged) —
whose definitions come from `/internal/upstreams/{name}/tools` (cached,
background refresh as before) and whose `execute` calls `/internal/call`.
The upstream list for agent-api still comes from `tenants.json`'s
`mcp_servers` keys (names only; URLs and tokens are ignored by agent-api and
live in `gateway.json`).

## 9. Harness engine

`Worker.tool_entries()` appends the gateway's `/internal/tools` entries (empty
list + a log line when the gateway is unreachable; the session still starts).
MCP-proxy skills are excluded from host-side entries. `sides.py`'s `mcp`
branch is removed: mcp calls never become `side_result` (PLAN.md 8).
