# MCP gateway (`mcp-gateway/`)

The gateway is the one MCP server the guest talks to, and the one MCP client
for every upstream server. It applies per-tenant policy, pins each session's
tool definitions, quarantines a tool whose upstream definition changes under a
pinned session, and relays upstream elicitations to the guest as questions.
PLAN.md section 9 has the design; this file is the contract and the service
as built. The guest's side of the wire (:5011, the forwarder, held calls) is
docs/PROTOCOL.md section 7.

```text
guest (VM) ──HTTP over vsock :5011──▶ host forwarder ──HTTP──▶ mcp-gateway ──MCP──▶ upstreams
                                      (agent-api, per session;   (own container)
                                       stamps identity headers)
worker (agent-api) ──HTTP──▶ mcp-gateway GET /internal/tools   (session-start tool list)
mcp-gateway ──HTTP──▶ agent-api POST /internal/gateway-events   (host log)
```

## HTTP surface

| Route | Caller | Behaviour |
|---|---|---|
| `POST /mcp` | guest via the forwarder | MCP streamable HTTP, stateless, JSON responses: no `initialize`; each POST is one JSON-RPC request answered by one JSON body. Methods `tools/list`, `tools/call`. |
| `GET /internal/tools` | worker at session start | The tenant's policy-filtered surface as `{"tools": [entry...]}` in hello shape (docs/PROTOCOL.md section 2); pins it for the session (the first call pins, later calls return the same pins). |
| `GET /healthz` | compose | `{ok: true}`, no auth |

Every other route is a 404 (the legacy engine's `/internal/upstreams/{name}/tools`
and `/internal/call` are gone).

### The MCP surface

`tools/list` = the tenant's promoted tools + three meta tools:

| Tool | Arguments | Result text |
|---|---|---|
| `search_tools` | `query` str, `limit` int (default 10) | JSON list `[{name, description}]` |
| `describe_tool` | `name` str | JSON `{name, description, inputSchema, pin}` (the pinned definition) |
| `call_tool` | `name` str (`upstream__tool`), `arguments` object | the upstream result |

Tool names everywhere are `{upstream}__{tool}` (`protocol.mcp`). Promoted
tools are also callable directly by that name. A `tools/call` result is an MCP
`CallToolResult` (`content` text items, `isError`). Gateway-side failures
(policy deny, quarantine, unknown tool, upstream timeout or unreachable,
concurrency limit) are results with `isError: true` and a plain message,
never JSON-RPC errors (those are for malformed requests only).

## Layout

| File | Role |
|---|---|
| `app/main.py` | Starlette app: `POST /mcp`, `/internal/*`, `GET /healthz`, `AuthMiddleware` (bearer + identity) |
| `app/tool_server.py` | Low-level MCP `Server` behind a stateless, JSON-response `StreamableHTTPSessionManager` |
| `app/internal.py` | `/internal/tools` |
| `app/gateway.py` | Core: session surface, pins, policy checks, calls, input-required relay, events |
| `app/upstreams.py` | One client per upstream (pooled httpx clients), tool-def cache `{tool: (defn, pin)}` with stale-while-revalidate for calls (pin time waits for an expired cache to refresh, so a new session never pins a replaced definition) and a failure backoff, `UpstreamCall` (one call in its own task) |
| `app/policy.py` | Allow/deny globs, default deny, `max_concurrent` limiter |
| `app/pins.py` | `pin_of`, per-session pin files (atomic write) |
| `app/schema.py` | Canonical definition, MCP to OpenAI conversion |
| `app/ssrf.py` | Upstream URL guard (moved from agent-api) |
| `app/events.py` | Fire-and-forget poster to agent-api `/internal/gateway-events` |
| `app/migrate_config.py` | `tenants.json` to `gateway.json` |

Wire constants, `{upstream}__{tool}` naming, the identity header names and
the id rule come from `harness/protocol/mcp.py`, which the guest, the host
forwarder and agent-api import too.

## Running

```bash
uv sync --all-packages                                  # once, at the repo root (the one workspace)
cd mcp-gateway && uv run pytest -q -n auto              # tests
uv run uvicorn --factory app.main:create_app --port 8090
docker build -f mcp-gateway/Dockerfile -t mcp-gateway .  # build context = repo root
docker compose up -d --build mcp-gateway
```

The image needs the `secondbrain-harness` package for `protocol` (canonical,
mcp, fsio, clock). Like agent-api, the gateway gets it as a member of the one
uv workspace (installed editable), so changes to `harness/protocol` are picked
up without a reinstall.
The build context is the repo root (the workspace). The Dockerfile copies the
root `pyproject.toml` + `uv.lock`, every member's `pyproject.toml` (the frozen
lock needs them), then `harness/protocol` and `mcp-gateway/app`, and runs
`uv sync --frozen --no-dev --package mcp-gateway`.
`mcp-gateway/Dockerfile.dockerignore` keeps the rest of the repo out of the
context.

Environment variables: `GATEWAY_CONFIG` (`/data/gateway.json`),
`GATEWAY_STATE_DIR` (`/data/gateway-state`), `AGENT_API_URL`
(`http://agent-api:8000`), `INTERNAL_TOKEN`, `MCP_ALLOWED_PRIVATE_HOSTS`
(`host.docker.internal`) and `LOG_LEVEL`. If the config file is missing, the
gateway starts with no upstreams, so every tenant sees only the meta tools.
agent-api's side: `MCP_GATEWAY_URL` (`http://mcp-gateway:8090`) and the same
`INTERNAL_TOKEN`, the shared bearer for every gateway route but `/healthz`
and for agent-api's `/internal/gateway-events` (empty = no check, dev only).
Both sides check and send it with `protocol.mcp.bearer_ok` / `bearer_header`;
the route paths are `protocol.mcp.INTERNAL_TOOLS_PATH` and
`GATEWAY_EVENTS_PATH`, a gateway event's identity keys
`protocol.mcp.GATEWAY_EVENT_IDENTITY`.

### Policy (`gateway.json`)

```json
{"upstreams": {"wcc-events": {"url": "...", "auth_token": "...", "timeout_s": 300,
                              "tool_defs_ttl_s": 300, "allow_private": false},
               "wcc-mentorship": {"url": "..."}, "wcc-analytic": {"url": "..."}},
 "tenants": {"default": {"upstreams": ["wcc-events", "wcc-mentorship", "wcc-analytic"],
                         "allow": ["*"], "deny": [], "promoted": [], "max_concurrent": 2}},
 "input_expires_s": 3600}
```

Globs (fnmatch, case-sensitive) match the namespaced name; deny wins over
allow. A tenant absent from `tenants`, or an upstream not in its `upstreams`
(or not configured), gives no tools (default deny). `max_concurrent` is per
session (default 2). `timeout_s` is an upstream call's total budget,
`tool_defs_ttl_s` how long its tools/list is cached. `input_expires_s`
(optional, default 3600) caps how long an input-required call is held.

### Migrating the MCP config

```bash
cd mcp-gateway
uv run python -m app.migrate_config ../agent-api/data/tenants.json > data/gateway.json
chmod 600 data/gateway.json
```

For each tenant, its `mcp_servers` become `upstreams` with `allow: ["*"]`.
`promoted` comes from an optional `promoted` key on the tenant. The default
tenant gets every upstream in the file. The default tenant is one of these:

- the entry with `is_default: true`
- the entry whose `api_key` equals `$API_SECRET_KEY`, when that is set
- otherwise a synthesized `default`

Tenants without MCP servers are left out, because default deny gives them no
tools anyway. Add `promoted` lists by hand afterwards, for example
`wcc-events__draft_event`. `gateway.json.example` shows the result for
`agent-api/tenants.json.example`.

## Request flow

1. `AuthMiddleware` checks every route except `/healthz`.
   - `Authorization: Bearer $INTERNAL_TOKEN` is required (constant-time compare). If it is wrong or missing, the response is 401.
   - `/mcp` and `/internal/tools` also need `X-SB-User`, `X-SB-Tenant` and `X-SB-Session`. Each value must pass `protocol.mcp.is_safe_id` (`[A-Za-z0-9_-]{1,64}`, the rule agent-api uses for user and session ids), or the response is 400. The values become pin-file path segments. The parsed identity is stored in the request's scope state; handlers read it from there.
2. `/mcp` goes to the SDK session manager in stateless mode with `json_response=True`.
   - Stateless sessions start in the Initialized state, so `tools/call` works without `initialize`.
   - The client must send `Accept: application/json`. The SDK returns 406 without it.
3. The handlers are registered directly in `Server.request_handlers` (see the SDK notes below). They read the identity from `server.request_context.request.state`.

### Session surface and pins

The first `/internal/tools`, `tools/list` or `tools/call` for an identity builds the pin table:

- It lists every upstream the tenant's policy allows, concurrently, through the per-upstream cache.
- It keeps every tool that policy allows. Each one is stored as `{pin, defn}` under its namespaced name.
- It writes `{state}/pins/{user}/{tenant}/{session}.json` atomically: temp file, fsync, rename, then fsync of the directory. Pin-file reads and writes run in a worker thread.

`defn` comes from `tool.model_dump(mode="json", exclude_none=True)`, limited to `name`, `description`, `inputSchema`, `outputSchema` and `annotations`. `pin = sha256_hex(canonical_json(defn))`. The OpenAI conversion always works on a copy.

Later calls use the stored table (memory first, then disk), so a restart keeps every pin and every stored `defn`. An upstream call checks these in order:

1. Policy, re-checked against the current `gateway.json`.
2. Pin present. If not: "tool not available in this session".
3. Sticky quarantine set.
4. Current definition hash against the pin, from the upstream's cache. If it differs, or the tool is gone upstream, the tool joins `quarantined`, the file is rewritten and a `quarantine` event is posted.

The upstream cache holds `{tool: (defn, pin)}`, computed once per fetch. When it is older than `tool_defs_ttl_s`, the cached copy is still served and one background refresh starts, so a changed definition is caught on the first call after that refresh lands. A tools/list that fails with nothing cached is remembered for 30 s; in that window callers fail at once instead of reconnecting.

### Calls

| Name | Behaviour |
|---|---|
| `search_tools` | Matches query words against name and description over the session's pinned tools that policy still allows; ranks by hit count. An empty query lists all. Returns `[{name, description}]` with the first line of the description, at most 200 characters. |
| `describe_tool` | Returns the stored `defn` as `{name (namespaced), description, inputSchema, pin}`, plus `quarantined: true` when it applies. |
| `call_tool` | Calls the inner `name`/`arguments`. `arguments` may also be a JSON string. |
| `upstream__tool` | Direct call. Only the tenant's promoted tools; any other name gets an error that says to use `call_tool`. |

The upstream call runs in its own asyncio task (`UpstreamCall`) on a
short-lived streamable-HTTP client session with the upstream's bearer, over a
long-lived httpx client per upstream (one for tools/list, one for tools/call),
closed at shutdown.
Redirects are not followed, because they would bypass the SSRF check, and the
SSRF check runs again before every connect.

`timeout_s` is the call's total budget from the moment it starts. When the
budget runs out, the task is cancelled and the guest gets an error result. The
upstream `_meta` is removed from the result, so an upstream cannot forge
`sb/*` keys. Every gateway-side failure is a `CallToolResult` with
`isError: true` and a plain message; the guest adds the
model-visible `Error:` prefix. JSON-RPC errors happen only when the SDK rejects a malformed
request.

### Input-required relay

When the upstream sends `elicitation/create`, the `UpstreamCall` callback parks on a future and the guest's POST returns:

- `content`: the message
- `_meta["sb/input_required"]`:
  - `request_state`: `secrets.token_urlsafe(24)`
  - `message`
  - `schema`: the elicitation's `requestedSchema`. For URL-mode elicitations it is `{}` and the URL is appended to the message.
  - `expires_in_s` = min(remaining budget, 3600, `input_expires_s`)

The held call releases its concurrency slot. A timer abandons it at expiry: the upstream gets `cancel`, any later elicitation on that call is answered `cancel`, and the task is cut 30 s later.

When the guest re-sends with `_meta["sb/input_response"]`:

- The `request_state` must exist and belong to the same (user, tenant, session) and the same tool name. Otherwise the result is "input request expired".
- The answer goes to the upstream as an `ElicitResult`. The call re-takes its slot without a limit check, so an answered question is never refused, and the gateway waits for the next outcome: the final result or another `sb/input_required`.
- A `request_state` is single-use.

The guest turns `sb/input_required` into a `question` whose deadline is
`min(question_timeout_s, expires_in_s)`; at the deadline it re-sends with
`action: "cancel"` (docs/GUEST.md "MCP over :5011"). While the question is
open the guest holds no connection to the gateway: the operation is
suspended, not running (its op timer is paused and it does not hold a
park). The held upstream call lives in gateway memory: a gateway restart
loses it, and the re-send gets "input request expired". It is all one
operation with one `call_id`.

### Events

The gateway posts `list` (tools/list, /internal/tools), `call` (upstream answered; `ok` = not `isError`, `ms`), `quarantine` and `error` (policy deny, not available, unreachable, timeout, concurrency limit, SSRF refusal, tools/list failure while pinning) to `{AGENT_API_URL}/internal/gateway-events` with the bearer, as `{user, tenant, session_id, op, tool?, upstream?, ok?, ms?, error?}`. Posts are fire-and-forget background tasks with a 5 s timeout; a failure is logged and never fails the call.

agent-api (`app/gateway/events.py`) appends the payload minus the identity as a `gateway` host event to that session's host log (docs/EVENTS.md "Host log"), through the same writer a live worker uses, and answers 202. No session directory → 404, dropped with a log line; a malformed body or unsafe id → 400; a bad bearer → 401.

## SDK notes (`mcp` 1.28.1, the same as agent-api's lock)

- **Raw `_meta` in, `_meta` out.** The `@server.call_tool()` decorator validates arguments against a tool cache that it fills by calling the list handler with no request. That would need an identity, so the gateway registers its own handler in `server.request_handlers[types.CallToolRequest]`. It reads `req.params.meta` (`RequestParams.Meta`, `extra="allow"`, so `model_dump(by_alias=True)` returns `sb/input_response`) and returns `ServerResult(CallToolResult(..., _meta=...))`. `Result.meta` is aliased to `_meta`, and the session serializes with `by_alias=True`.
- **Stateless.** `StreamableHTTPSessionManager(stateless=True)` creates a `ServerSession(stateless=True)`, which starts in the `Initialized` state. A bare `tools/call` POST is answered, and the tests cover it (`test_tools_list_without_initialize`).
- **Identity.** In both modes the transport attaches the Starlette `Request` to each message (`ServerMessageMetadata.request_context`), and handlers read it through `server.request_context.request`.
- **Client elicitation.** `ClientSession(elicitation_callback=...)` receives `(RequestContext, ElicitRequestFormParams | ElicitRequestURLParams)` and returns `ElicitResult`. The callback is awaited inside the client's receive loop, so it can wait on a future for as long as needed. The upstream's SSE stream stays open, and the httpx read timeout is set to `timeout_s + input_expires_s`. Without a callback the SDK answers an elicitation with an error.
- `streamablehttp_client` is deprecated in 1.28. The gateway uses `streamable_http_client(url, http_client=httpx.AsyncClient(...))`.

## Further rules

- **Unlisted upstreams.** If an allowed upstream cannot be listed when a session pins, its name goes in the pin file's `unlisted` list. Each later call for that session tries to list it again (all such upstreams concurrently; inside the failure backoff this costs nothing) and pins its tools when it answers. The `error` event is posted only for the first failure. No existing pin changes.
- **Tools added later.** A tool absent from the pin table (added upstream after the session pinned) is not callable in that session ("tool not available in this session"); new sessions pick it up. A quarantined tool answers "tool definition changed since this session started (quarantined)"; new sessions pin the new definition.
- **`describe_tool` extras.** It adds `quarantined: true` for a quarantined tool. Its `inputSchema` is the stored one, and `$schema` is kept.
- **Direct calls.** A direct `tools/call` for a non-promoted `upstream__tool` is refused, and the error says to use `call_tool`.
- **Hello descriptions.** Hello entries for promoted tools carry the upstream description verbatim (no `[via X MCP]` prefix).
- **tenants.json MCP keys.** agent-api accepts tenants.json's `mcp_servers` (and `promoted`) and drops them at load with a warning (a tenant with `mcp_servers` and no `local_skills` gets `local_skills: []`; docs/HOST.md "Tenants"); `migrate_config` still reads them to build `gateway.json`.
