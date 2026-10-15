# SecondBrain Agent Harness — implementation plan (draft 6: microVM · MCP gateway · Temporal)

Scope: replace the agent loop inside `agent-api` with the harness from the v4.1
diagram, run it inside a **microVM per session**, put an **MCP gateway** between the
agent and every tool server, and drive long-running work with **Temporal**.
The FastAPI surface, the iPhone contract, the session directories, the skills and
the tenants file stay.

**Security is out of scope for this plan.** Isolation hardening, egress policy,
secret injection, identity binding, DLP and audit integrity are the job of the
NVIDIA OpenShell layer. Section 11 lists what this plan assumes OpenShell provides
and the hooks the harness exposes to it; nothing else in the plan designs a security
control. Where the v4.1 diagram draws one (token broker, bind_check, launcher, mTLS),
read "OpenShell".

Build steps are **milestones M0–M6**. The repo's old `Phase 1–5` numbering is retired.

---

## 0. Shape of the system

```text
 iPhone / avatar page / WCC clients
        │ HTTPS + SSE (Cloudflare tunnel)
        ▼
 agent-api  (FastAPI, one container)                       ──▶ Temporal
   routes · tenants · sessions dir · host skills                (server + UI on the box;
   HOST HALF per session: worker · vsock endpoints ·            AWS in the cloud)
     LLM proxy :5010 · gateway forwarder :5011 ·
     state sync · sandbox manager (Firecracker)
        │ vsock                 │ HTTP                 │ HTTP (internal net)
        ▼                       ▼                      ▼
 microVM per session        mcp-gateway            vLLM (secondbrain-llm)
   GUEST HARNESS              tool server: the MCP server guests call
   (stdlib, event-sourced)    upstreams: clients to wcc-events · wcc-mentorship ·
   /workspace volume                 wcc-analytic · future servers
```

Four tool sides, decided per tool at `session_start`:

| Side | Runs | Examples |
|---|---|---|
| `guest` | inside the VM, by the harness operation manager | `bash`, `read`, `write`, `edit`, `grep`, `glob`, `ls`, `todo`, `question` |
| `host` | in agent-api, by the worker, as today's `BaseSkill.execute()` | `web_search`, `email`, `github_cli`, `gitlab_cli`, `avatar_control`, `remember`, `forget` |
| `device` | on the iPhone, via SSE and `POST /tool-result` | `calendar`, `reminders`, `contacts`, `clipboard` |
| `mcp` | on an upstream MCP server, through the gateway | `wcc-events__draft_event`, … |

The guest harness treats all four the same way: a `tool_call` event, an operation with a timeout, a `tool_result` event. Only the `guest` side executes in the VM; the other three are **host-originated operations**: the guest emits the call, the worker carries it out (or relays it) and answers with a control message.

What this buys SecondBrain:

1. **A real workspace.** The agent can write files, run code and tests, and keep a tree per session, without any of it touching the server.
2. **Tasks that outlive the request.** A device result, a 45-minute MCP pipeline (`wcc-events.timeout_s = 2700`) or a question the phone answers an hour later no longer hangs on one SSE connection and one in-process `asyncio.Event`. A Temporal session workflow keeps the task open; the VM is snapshotted while it waits.
3. **One tool gate.** The gateway holds the upstream list, the per-tenant policy and the pinned tool definitions. The in-process `MCPProxySkill` and its per-request connections go away.
4. **Idempotent inputs, a stable prompt prefix, full history, one loop for guest and tenant, replay** — as in draft 5 (sections 4–8).

---

## 1. Ground rules (do not re-open)

| Rule | Consequence for the build |
|---|---|
| One language, standard library only, one process in the guest | Python 3.12 PEX from `harness/guest/` + `harness/protocol/`; zero third-party imports, enforced by a test. |
| Guest never listens; it connects out to CID 2 | Control, bulk, LLM and gateway channels are guest-initiated vsock connections. |
| Every decision is an event; state = fold(events) | Replay never calls vLLM, never executes a tool, never calls the gateway. |
| The host is the canonical store | Guest log = buffer; durable = host ack **after** the state sync has appended to the session directory. |
| Committed prefix never rewritten | Corrections appended; compaction is a turn + event; the system block is fixed per session. |
| Config arrives in the first vsock message | No config files in the image; `hello` carries everything. |
| Tools are built in; the gateway is the only path to MCP | Guest tool registry = `guest` tools + the host/device/mcp names from `hello`; no plugin system. |
| Inputs idempotent by caller id | Every POST carries `input_id`; the session workflow dedupes task signals by it. |
| Wire compatibility with the iPhone | SSE `token`, `tool_call`, `avatar_command`, `done` unchanged; `seq`, `question`, `error` added. |

---

## 2. Decisions (defaults proposed; confirm before M1)

| # | Decision | Default | Why |
|---|---|---|---|
| D1 | Guest language | Python 3.12, stdlib; `socket(AF_VSOCK)`, `http.client`, `json`, `subprocess`, `threading`, `queue`, `hashlib` | As the diagram. |
| D2 | Code location | `harness/protocol/`, `harness/guest/` (built into the VM image); `agent-api/app/host/` (worker, vsock, proxies, sandbox, state sync); `mcp-gateway/` (own container); `flows/` (Temporal workflows, activities, schedules, worker) | The guest image is built from two directories; everything else is a normal service. |
| D3 | Engine switch | `AGENT_ENGINE=harness` |  |
| D4 | Canonical store | `Sink` interface; `local_dir` sink = the session directory on the bind-mounted `data/` volume. No bucket. | The store this deployment has. |
| D5 | Guard slot ⑦ | `before_tool` priority chain, pass-through, still emits `guard_decision` | Policy later; OpenShell holds the enforcement. |
| D6 | Async tool calls | Coordinator built for it; `async_tools=False` default | Qwen3-14B is not trusted with placeholders yet; measured in M6. |
| D6a | **Guest runtime from M1** | **Firecracker microVM from the first milestone; there is no Docker box.** Unix sockets named after the vsock ports are the test double for the transport (fake host, fake vLLM, guest as a subprocess) and nothing else. The build machine has no KVM, so every live run is on the server. | One runtime to debug; the image, the sandbox manager and the vsock transport are built with the loop instead of after it. |
| D7 | Provider wire shape in the guest | OpenAI chat-completions as vLLM serves it, streamed over :5010; `llm_response` records the assembled message verbatim | The guest speaks the one shape the model speaks. The proxy records usage and enforces budgets. |
| D8 | Guest concurrency | One coordinator thread per session, one transport-writer, one log-writer; I/O threads feed a `queue.Queue` | Single writer without asyncio. |
| D9 | Reviewer stratum | Placeholder in the `before_tool` chain | No design yet. |
| D10 | Audit UI | Traces endpoints read from the fold; iOS debug viewer unchanged | Same data, better source. |
| D11 | **Control plane: Temporal** | One long-lived `SessionWorkflow` per session (workflow id `{user}/{tenant}/{session_id}`); each `task` input is a signal to it; the harness run is a heartbeating activity; questions are signals the workflow waits on; schedules are Temporal Schedules. Temporal Server self-hosted on the box (dev server with a SQLite file on the `data/` volume; Postgres-backed `auto-setup` when it moves off the home server); Python SDK `temporalio`; the worker runs inside the agent-api process. **A session is one workflow; a VM spans a session.** | Temporal gives durable timers, signals, heartbeats with resume details, retries and schedules as library calls, and its UI shows every task. No ASL, no task tokens, no emulator. |
| D12 | **Questions and human waits** | `question` is a built-in tool → SSE `question` event → `POST /chat {answer_to}` → `ui_response`. Deadline `question_timeout_s` (phone 300 s, guest 60 s). On expiry with `ui_attached=false` the task **parks**: the VM is snapshotted and the workflow waits on an `answer` signal with a durable timer (`answer_wait_s`); the phone gets the question on reconnect; `POST /chat {answer_to}` signals the workflow, which restores the VM and resumes. Three unanswered → `settle{escalated}`. | Long human waits stop costing a running VM and a held HTTP connection. |
| D13 | Subagents | Not built, placeholder; `task` tool not offered | No use case yet. |
| D14 | Diagnostics | `log` live messages to the worker; SSE when `?debug=1`; never in the event log | As the diagram. |
| D15 | Migration order | Default tenant (owner's phone) → guest avatar → WCC tenants | Lowest blast radius first. |
| D16 | Tool list per session | Effective list fixed at `session_start` from tenant `local_skills`, the gateway's policy-filtered `list_tools` for the tenant, and the guest built-ins; recorded in `session_start.tools` with each tool's side. **Keyword prefilter retired.** | A per-message tool list changes the chat-template prefix and defeats the prefix cache; the allowlist is the gate. |
| D17 | Streaming | vLLM `stream: true` through the proxy; deltas to the client as `token`; `LLM_STREAM=false` falls back to one call per turn | Real streaming for TTS; a flag until streamed hermes tool calls are verified on vLLM 0.11. |
| D18 | VM boot | Snapshot restore of the image with the harness idle before connecting out; rootfs boot fallback; **per-session VM snapshot** at park and at idle; destroy after `session_idle_s` (1800) or on session `complete`/`failed` | < 200 ms to `ready`; a parked task costs nothing. |
| D19 | Build | change to pants.  `harness/` built by a `make image` target: PEX → rootfs → snapshot | use Pants. |
| D20 | **MCP gateway** | Own container `mcp-gateway`, Python, the `mcp` SDK already in the lock file. `tool_server.py`: one MCP streamable-HTTP server the forwarder reaches. `upstreams.py`: one client per upstream from `tenants.json`'s `mcp_servers`. Policy = tenant allowlist. Pins = sha256 of each upstream tool definition at session start. Meta-surface `search_tools`, `describe_tool`, `call_tool` plus **promoted direct tools** per tenant. | Replaces `MCPProxySkill`; section 9. |
| D21 | Host skills | Existing `BaseSkill` server skills run on the host, called by the worker; their credentials (email, gh) never enter the VM | No rewrite of nine skills; the VM stays clean. |
| D22 | Workers | `uvicorn --workers 1` stays; the Temporal worker (one task queue, `secondbrain-agent`) runs on the same asyncio loop inside that process; host-side threads only for vsock I/O | Coordinator state and vsock endpoints are per process; a second agent-api replica is a second Temporal worker on the same queue, later. |
| D23 | **Memory store** | **Local files, as today.** User-tier memory = `data/users/{user}/memory/*.md` + `MEMORY.md` index; session-tier memory = `{session dir}/memory/`; profile = `data/users/{user}/profile.json` + `profile.md`. The existing `MemoryStore` and `UserProfile` classes stay; recall stays keyword overlap on the description line. No bucket, no database. The guest never opens these files: it gets the index in `hello`, recalled bodies in each input's `attachments`, and writes through the host-side `remember`/`forget` skills. | The files are already there and already work; moving them to a bucket adds a credential and a network hop for nothing until there is a second node. |

---

## 3. Target layout

```text
harness/
  protocol/                 stdlib only, shared by guest, host, gateway, tests
    messages.py             envelope (session_id, seq | cseq), acks, control + live messages
    events.py               event schema, versions, validation rules
    canonical.py            canonical JSON, sha256, derived ids
    fold.py                 fold(events) → State; checkpoint verify; replay verifier
    allowlist.py            effective tool list and sides from hello
    ports.py limits.py fsio.py usage.py clock.py uds.py version.py   shared definitions, one home each
  guest/
    main.py                 idle-wait (snapshot point) → connect out → hello → run → settle
    transport/  eventlog.py  context/  provider/  tools/  ops/
    coordinator.py  hooks.py  session/
  image/                    Makefile: PEX → rootfs (RO) → Firecracker snapshot
  docs/                     PROTOCOL.md  EVENTS.md  REPLAY.md  GATEWAY.md  FLOWS.md
agent-api/app/
  host/
    worker.py               Temporal activities: prepare_vm, run_task (heartbeating), park_vm, deliver
    vsock_server.py         one UDS per port per session; session identity from the endpoint
    llm_proxy.py            :5010 → vLLM; stream pass-through; budgets; usage events
    gateway_forwarder.py    :5011 → mcp-gateway (HTTP); tenant and session asserted by the forwarder
    state_sync.py           acks after append; checkpoints; bulk → session dir; projections
    sandbox/firecracker.py  boot, snapshot, restore, destroy over the Firecracker API; sandbox/local.py = guest as a subprocess, tests only
    sides.py                host-side execution of BaseSkill tools; device relay; mcp relay
    stream.py               events → SSE (token, tool_call, avatar_command, question, done, seq, error)
    sinks/                  sink.py, local_dir.py
  routes/                   chat.py (both engines), tool_result.py, guest.py (thin), sessions.py
  agent/loop.py             legacy engine until M6
mcp-gateway/
  app/tool_server.py        the MCP server guests call: list_tools, search/describe/call, promoted direct tools
  app/upstreams.py          clients to the upstream MCP servers (from app/mcp/client.py); tool-definition cache
  app/pins.py               per-session pins of tool definitions; quarantine on change
  app/policy.py             tenant allowlist, promotion rules, quarantine
  app/events.py             gateway events → agent-api (HTTP) for the session log
flows/                      Temporal (python package, imported by agent-api)
  session_workflow.py       SessionWorkflow: task signals, answer signals, park/resume, continue-as-new
  activities.py             activity signatures and timeouts (implemented in app/host/worker.py)
  schedules.py              Temporal Schedules from tenants.json (morning brief, inbox sweep)
  worker.py                 starts the Temporal worker in the agent-api process
  temporal/                 compose service: temporal dev server + UI; data/temporal.db
```

Session directory (existing layout kept, extended):

```text
{root}/{user}/{tenant}.{session_id}/
    session.json             metadata + forward-only state
    events/000001.jsonl      event segments (canonical) · checkpoints.jsonl
    history.jsonl            projection (kept one release) · logs/trace.jsonl projection
    tickets/                 projection: one per task at settle, with the workspace manifest
    workspace/               pulled from the VM at settle (manifest) · .spill/
    vm/                      workspace.img (virtio-blk) · snapshot/ (parked or idle VM)
    memory/  ipc/            unchanged
```

User-tier files, unchanged: `data/users/{user}/profile.json`, `profile.md`, `memory/*.md`, `memory/MEMORY.md` (D23).

Compose additions: `temporal` (gRPC 7233 internal; its bundled UI on 8233, localhost only), `mcp-gateway` (on `llm_internal` and `agent-net`), agent-api gets `/dev/kvm` and `/dev/vhost-vsock` devices and a `vm/` cache volume for the base snapshot.

---

## 4. vsock control protocol (`docs/PROTOCOL.md`)

### 4.1 Channels

| Port | Direction | Content |
|---|---|---|
| :5000 control | guest → host connect; both directions after | control messages, events, acks, live messages — JSONL |
| :5001 bulk | guest → host connect; both directions after | spill files, workspace manifest + files at settle, log return on resume |
| :5010 LLM | guest → host | HTTP/1.1 to the LLM proxy, SSE |
| :5011 gateway | guest → host | HTTP/1.1, MCP streamable HTTP to the gateway forwarder |
| 5020–5029 | — | reserved |

No web proxy: the guest has no network device; `web_search` and `webfetch` are host-side skills. Test transport: Unix sockets named after the ports, used by the unit tests and the fake host only; every live run is a microVM (D6a).

### 4.2 Sequenced messages

Guest → host: the events themselves, `{"session_id", "seq", "ts", "schema", "type", "body"}`. Host → guest: `{"session_id", "cseq", "type", "body"}`. Acks either way: highest contiguous. `seq`/`cseq` belong to the session, survive reconnects and VM restores; each side resends from `ack + 1`. Live messages (`ready`, `delta`, `log`, `bulk_ref`) are unsequenced and never stored.

### 4.3 Control messages host → guest

| type | body |
|---|---|
| `hello` | `config` (model alias, limits, tools with sides, ports, `async_tools`, `question_timeout_s`, `ui_attached`, `harness_version`), `session_id`, `principal` (user, tenant), `system_block` (base prompt, profile, memory index), `resume`, `at` |
| `task` / `follow_up` / `steer` | `input_id`, `text`, `attachments` (recalled memories for the preamble, computed on the host) |
| `ui_response` | `input_id`, `question_id`, `answer` |
| `side_result` | `call_id`, `side` (`host`/`device`/`mcp`), `content`, `ok` — the answer to a host-originated operation |
| `park` | `reason` — finish the current operation, checkpoint, go idle for snapshot |
| `abort` | `reason` |
| `heartbeat` | `at` — every 60 s → `clock{host_heartbeat}` |

### 4.4 Messages guest → host

Events (section 5), plus live `ready`, `delta{turn_id, kind, text}`, `log`, `bulk_ref`. A `tool_call` whose side is not `guest` is also a **request to the host**: the worker runs or relays it and answers with `side_result`.

### 4.5 Reconnect, park, resume

Stream drop: coordinator pauses, transport reconnects with backoff, both sides resend from ack+1. Park (D12, D18): worker sends `park`; guest finishes in-flight guest operations, writes a `checkpoint`, emits `parked`, stops connecting; worker snapshots the VM. Resume: restore the snapshot, the guest reconnects, the worker sends pending control messages (`ui_response` first). Guest or VM death: worker restores the base snapshot on the same `workspace.img`, sends `hello{resume: true}`, streams the acked log back on :5001; open calls get `op_state{lost}`; `max_resumes` 1.

---

## 5. Event schema (`docs/EVENTS.md`)

Every event: `session_id`, `seq`, `ts`, `schema`, `type`, `body`. `ts` is data, never a fold input; time inside the fold comes from `clock` events. Ids derived: `turn_id`, `question_id`, operation ids, `ticket_id` = `h(user, tenant, session_id, seq)`; `call_id` is vLLM's, verbatim. What turn T saw = events with `seq ≤ T.includes_through_seq`.

| type | body | writer |
|---|---|---|
| `session_start` | user, tenant, workflow run id, config hash, `system_hash`, `tools` (name, side, pin), harness version, image version, `resumed` | guest main |
| `input` | input_id, kind (`task`/`follow_up`/`steer`/`ui_response`), text, dedupe | inbox |
| `clock` | kind (`host_hello`/`host_heartbeat`/`turn_clock`/`grace_*`/`idle_timer`/`question_deadline`/`wall_clock`), at | coordinator |
| `turn_start` | turn_id, type, `includes_through_seq`, model, params, `request_hash`, preamble hash, `workspace_tree_id` | coordinator |
| `llm_response` | turn_id, message verbatim, finish_reason, usage, provider response id | provider |
| `tool_call` | turn_id, call_id, name, side, args, state RUNNING | submit |
| `guard_decision` | call_id or question_id, decision, reason, policy id | guard / coordinator |
| `op_state` | call_id, state (`queued`/`running`/`done`/`failed`/`timeout`/`lost`), pid or side ref, duration_ms | ops / resume |
| `tool_result` | call_id, content verbatim (truncated at `spill_threshold`), spill_ref, rewrite (after_tool) | ops / hooks |
| `wake` | reason, result_ids[], input_ids[] | coordinator |
| `question` | question_id, call_id, text, options, deadline | tools |
| `parked` | checkpoint seq, reason | coordinator |
| `compaction` / `memory` / `bulk` / `checkpoint` / `error` | as draft 5 | — |
| `settle` | task input_id, outcome (`done`/`failed`/`timeout`/`escalated`), answer, usage totals, workspace manifest | coordinator |

Host-session events (written by the worker into a sibling log `events/host-*.jsonl`, linked by session): `workflow{task_signal/activity_start/heartbeat/completed/parked}`, `side_call{host/device/mcp}`, `gateway{list/call/quarantine}`, `vm{restore/snapshot/destroy}`, `usage`. These are the diagram's purple ▶ wiring.

Schema rules as draft 5, plus: a `tool_call.side` must match `session_start.tools`; `side_result` for an unknown or terminal `call_id` is recorded `input{dedupe: late}` and ignored.

Confirmed fold rules (M0 review): a first `tool_result` for a call whose placeholder was already committed must carry `late: true` (the strict reading: `late` always equals the state-derived predicate); `queues.follow_up` and `queues.steer` are both lists of seqs, so logs with those inputs hash differently from the first fold draft (no golden has them).

Canonical form for hashing (`protocol/canonical.py`): sorted keys, no whitespace, NFC strings; ints as digits, finite floats by their shortest round-trip text (tool arguments carry floats), NaN and infinity rejected; a body that cannot be canonicalised is a schema error. Ids: `derive_id(user, tenant, session_id, seq, kind)` → `<kind>_<24 hex>`.

---

## 6. The coordinator (guest)

As draft 5 section 6, with these additions: host-originated operations are `op_state{running, side}` until `side_result` arrives or `op_timeout_s` for that side expires (device: `TOOL_TIMEOUT` 60; host skill: 120; mcp: the upstream's `timeout_s` relayed in `hello`); `park` is honoured only at a turn boundary or while every running operation is host-originated (their results are replayed on resume); the idle timer (10 min) is a `clock` + `log` in sync mode.

Limits from `hello` (tenant keys): `max_tools` → operations per task, `max_tokens`, `wall_clock_s`, `op_timeout_s` by side, `max_turns`, `spill_threshold` 2000, spill quota.

---

## 7. Context assembly (guest)

As draft 5 section 7: system block fixed at `session_start` (tenant prompt without `{current_time}`, profile, memory index, tool definitions); committed prefix with tool messages; per-turn preamble (time from `clock{turn_clock}`, recalled memories delivered in the input's `attachments`, `[new memory]` lines); compaction as a turn; memory events with `derived_from`. `workspace_tree_id` per turn is the shadow-git tree hash of `/workspace`.

**Memory stays on local files (D23).** Two tiers, both markdown with frontmatter under the bind-mounted `data/` volume: user-tier under `data/users/{user}/memory/` (shared across the user's tenants and sessions) and session-tier under the session directory's `memory/`. The host reads them at two points only: at `session_start`, to put the two `MEMORY.md` indexes and the profile block into `hello.system_block`; and at each input, to run `MemoryStore.recall(message)` and attach up to three bodies to the `task`/`follow_up` message. Writes happen only through the host-side `remember`/`forget` skills; each write is a `memory` host event carrying the file's sha256 and the `call_id` it came from, and a compaction summary is written to the session tier with `derived_from = summary_turn_id`. Replay therefore needs the log, not the files: the index hash in `session_start` and the attached bodies in each `input` fix what the model saw. A bucket-backed `MemoryStore` can be added later behind the same class without touching the guest.

---

## 8. Tools

**Guest built-ins**: `read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `glob`, `ls`, `todo`, `question`, `skill`. Operation manager as the diagram: thread pool for file tools, process groups for bash, per-file queue, abort kills the tree, spill files on :5001, tree id after mutating ops.

**Host side** (`app/host/sides.py`): the worker calls `registry.execute_server_tool(name, args, allowed)` in an asyncio task and answers `side_result`. `remember`/`forget` write under the session or user directory as today and the worker records a `memory` host event with the sha256.

**Device side**: `tool_call{side: device}` → SSE `tool_call{id, name, arguments}` (unchanged payload) → `POST /tool-result` → `side_result`. Survives a dropped stream and a restart: the open call is re-emitted to the next stream that attaches.

**MCP side**: `tool_call{side: mcp}` → the guest itself calls the gateway over :5011 (`call_tool` or a promoted direct tool); the forwarder stamps user, tenant and session on the way. The result is the operation's result; no `side_result` needed.

**Pipeline**: validate (schema; one repair turn for malformed JSON, never a silent `{}`) → allowlist (`session_start.tools`) → `before_tool` chain → submit → operation → `tool_result` → `after_tool` chain (`sanitize`, avatar rewrite; appended). Doom loop and `finish_reason = length` as draft 5.

**Guest tenant (avatar)**: a tenant profile with `auth: origin`, `local_skills: ["avatar_control"]` (host side), limits as today; same engine, thin route; `guest.py`'s loop deleted in M6.

---

## 9. MCP gateway (`mcp-gateway/`, `docs/GATEWAY.md`)

> As built, `docs/GATEWAY.md` is authoritative where it differs from this design: deny wins over allow (not first match), promoted lists live in `gateway.json` (not `tenants.json`), and the per-session pin table is persisted to disk under the gateway's state dir (it is not rebuilt from `session_start`, which holds only the promoted and meta pins).

**Serving the guest** (`tool_server.py`; the diagram's south side) — one MCP streamable-HTTP server. Every request arrives through the forwarder with `X-SB-User`, `X-SB-Tenant`, `X-SB-Session` set by agent-api (the identity assurance behind those headers is OpenShell's). Surface:

| Tool | Behaviour |
|---|---|
| `search_tools(query, limit)` | policy-filtered names and one-line descriptions for the tenant |
| `describe_tool(name)` | full schema; the pinned definition |
| `call_tool(name, arguments)` | validates against the pin, forwards to the upstream, returns content |
| promoted direct tools | tenant `promoted: [...]` in `tenants.json`; listed as first-class tools in `list_tools` so the model calls them without the meta-surface |

`list_tools` for a tenant = promoted tools + the three meta tools. The guest's `session_start.tools` records exactly this list with pins, so a session's tool surface is fixed and replayable.

**Upstream servers** (`upstreams.py`, `pins.py`; the diagram's north side) — one client per upstream (today: `wcc-events`, `wcc-mentorship`, `wcc-analytic`; config moves from `tenants.json`'s `mcp_servers` to the gateway's config, same keys). The existing `app/mcp/client.py`, `schema.py` and `ssrf.py` move here. Tool definitions are cached with `tool_defs_ttl_s` and **pinned per session**: `pin = sha256(canonical definition)` at the session's first `list_tools`; a later refresh that changes a pinned tool marks it `quarantined` for sessions holding the old pin (calls return a clear error, new sessions get the new pin). `input_required` from an upstream (the WCC servers use it for approvals) is relayed as an MCP elicitation to the guest, which turns it into a `question`.

**Policy** (`policy.py`): per tenant: upstream names allowed, tool allow/deny globs, promoted list, per-call timeout from the upstream config, max concurrent calls per session (2). First match wins; default deny for unknown upstreams.

**Events**: the gateway posts `gateway{list, call, quarantine, error}` to agent-api (`POST /internal/gateway-events`, internal network) which writes them to the session's host log.

**Stateless**: no session affinity; the pin table is keyed by session and rebuilt from the session's `session_start` if the gateway restarts.

---

## 10. Temporal (`flows/`, `docs/FLOWS.md`)

Temporal owns orchestration: when a task starts, how long it may run, what happens when the worker dies, how long a question may wait, when a schedule fires. The harness event log owns what the model saw. The two never duplicate each other: the workflow history holds signals, activity attempts and timers; the session log holds turns, calls and results.

**SessionWorkflow** — one per session, started with `signal_with_start` by the first task, workflow id `{user}/{tenant}/{session_id}`:

```text
loop:
  wait for a `task` signal (or a `schedule` signal)          ─ dedupe by input_id
  prepare_vm        activity  retry 3×, start_to_close 60 s   ─ restore base or parked snapshot; hello
  run_task          activity  heartbeat 60 s, heartbeat_timeout 120 s,
                              start_to_close = wall_clock_s, retry 1× with heartbeat details
                    returns settle{outcome} | parked{question_id}
  if parked:        park_vm activity → wait for `answer` signal with timer answer_wait_s
                    answer → run_task(resume=True) ; timer → deliver(escalated)
  deliver           activity  retry 3×  ─ workspace manifest, ticket projection, final SSE `done`
  continue-as-new after 50 tasks or when history passes 10 000 events
```

- **Signals**: `task{input_id, text, kind}`, `answer{question_id, text}`, `abort{reason}`. A redelivered POST sends the same `task` signal; the workflow keeps a set of seen `input_id`s and ignores the duplicate. **Queries**: `status` (idle, running, parked, which question). **Update** (optional, M5): `submit_task` returns when the task is accepted, so a client that cannot hold SSE still gets an acknowledgement.
- **Activities** are the host half (`app/host/worker.py`): `prepare_vm`, `run_task`, `park_vm`, `deliver`. `run_task` relays events, heartbeats every 60 s with the last acked `seq` as heartbeat details; if the worker process dies, Temporal retries the activity once and the retry resumes the guest from that `seq` (section 4.5), so a crash never reruns a model turn. M0 spike caveat: the SDK throttles heartbeats and flushes buffered ones only on a raised failure, so after a SIGKILL the retry may see a `seq` well behind the guest's; the resume path must therefore treat the heartbeat `seq` as a floor and take the guest's acked log as the truth, which section 4.5 already requires.
- **Questions** (D12): the guest's `question` with `ui_attached=false` makes `run_task` return `parked`; the workflow parks the VM and waits on `answer` with a durable timer. The phone's `POST /chat {answer_to}` finds the session's workflow and signals it. `answer_wait_s` per tenant (phone: 24 h; guest: 60 s).
- **Schedules**: `schedules.py` creates one Temporal Schedule per entry in `tenants.json` (`schedules: [{cron, prompt, session}]`). A schedule action cannot signal-with-start (SDK 1.34 finding, M0 spike), so the action starts a one-shot `ScheduleTickWorkflow` whose `signal_session` activity signal-with-starts the session workflow with `kind: schedule` and `input_id = {schedule_id}-{fire_time}`. Catch-up policy: skip missed runs. Example: the morning brief at 07:00 into session `daily`.
- **Chat path**: `POST /chat` appends `input` to the session inbox, `signal_with_start`s the workflow, and streams the session's live events; the SSE stream is a projection and never the thing the task depends on. `GET /sessions/{id}/events?after_seq` catches up; `GET /sessions/{id}` adds the workflow `status` query.
- **Server**: compose service `temporal` running the dev server (`temporalio/temporal` CLI image, verified Server 1.32 / UI 2.54) with `--db-filename /data/temporal.db` on the bind-mounted volume and `--namespace secondbrain`; the dev server bundles the UI on :8233 (localhost only), so a separate `temporal-ui` service is needed only after the move to `auto-setup` + Postgres; retention 30 days (set on the namespace; the dev default is 24 h). Moving off the home server means `temporalio/auto-setup` with Postgres and the same worker code.
- **Worker**: `flows/worker.py` starts a `temporalio` worker on task queue `secondbrain-agent` in the agent-api process at startup (behind `AGENT_ENGINE=harness`); workflow code in `flows/session_workflow.py` is deterministic and imports nothing from `app/` (the SDK's workflow sandbox enforces it); activities are the only place that touch the VM, the session dir or the phone.

What is built for this (M0 delivered the `flows/` package with 12 passing tests on the time-skipping server, the local CLI server and a docker dev server; `harness/docs/spikes/temporal.md` has the findings): the `temporalio` dependency and the compose service; `SessionWorkflow` with signals, queries, timers and continue-as-new; the four activities; schedules from `tenants.json`; the worker bootstrap; workflow tests on the SDK's time-skipping test server; a `temporal` CLI runbook (list, describe, signal, terminate, reset) in `docs/FLOWS.md`.

---

## 11. Security layer: NVIDIA OpenShell (out of scope here)

Assumed provided by OpenShell, not designed in this plan: VM isolation hardening and jailing, the egress policy for the guest (only the five vsock ports), credential and token injection at the LLM proxy and at the gateway's upstream clients, identity binding between a VM and its session (what the diagram calls `bind_check`), DLP on tool results, and tamper-evidence of stored logs.

Hooks the harness exposes so OpenShell can own those: the sandbox manager's launch and destroy calls (`sandbox/firecracker.py` is an interface with one backend; OpenShell's runtime is the second backend if it brings its own VM); the per-session UDS endpoints (one per port, so policy attaches per session); the forwarder's three identity headers; the upstream clients' credential lookup (a function, not a file); and the host event log. Confirm each against the OpenShell documentation at M0 and record the outcome in `docs/SECURITY-INTEGRATION.md`.

---

## 12. Deterministic replay

The ten invariants of draft 5 section 10, unchanged, plus: `tool_call.side` and `session_start.tools` are in `State` so replay rejects a call to a tool that could not have been offered; host-originated results replay from `tool_result` like any other; the gateway's pins are part of `session_start`, so a replay against a changed upstream still folds.

---

## 13. Milestones

Sizes: S ≈ days, M ≈ 1–2 weeks, L ≈ several weeks of one person.

### M0 — contracts and spikes (M)
`harness/protocol/` with tests and the purity test; `docs/PROTOCOL.md`, `EVENTS.md`, `REPLAY.md`; three golden logs (text answer; device tool with a late duplicate; mcp tool via gateway). Spikes recorded as findings: Temporal dev server on the box, a heartbeating activity that survives a worker restart, and a 24-hour timer on a parked workflow; Firecracker + vsock on the RTX 5080 server (`/dev/kvm` inside the agent-api container); OpenShell integration points (section 11). **Exit:** fold passes goldens; validator rejects every schema rule; three spike notes in `docs/`.

### M1 — the loop in a microVM, four sides (L)
`harness/guest/`: main (idle-wait, connect out, hello, run, settle), transport over `AF_VSOCK` with a Unix-socket connector for tests, inbox, coordinator, log-writer, context builder, provider over :5010 (non-streamed), guest built-ins, operation manager. `agent-api/app/host/`: worker (no Temporal yet: direct call), per-session UDS endpoints (the same listener serves Firecracker's host-side vsock sockets and the test double), LLM proxy, `sides.py` for host and device tools, state sync, `stream.py`, `AGENT_ENGINE` switch, projections for `history.jsonl` and `trace.jsonl`. `sandbox/firecracker.py`: boot from rootfs over the Firecracker API (vsock device, read-only rootfs, `/workspace` drive, kill); `harness/image/`: zipapp of guest + protocol, rootfs build, kernel fetch. Fakes: scripted vLLM server, fake host, guest as a subprocess. **Exit:** on the server, the owner's phone runs a calendar query and a web search through a Firecracker VM on `harness`; the no-TAP VM cannot reach the network; redelivered POST runs once; goldens reproduced live by the fake-host end-to-end test; existing tests green under both engines.

### M2 — context and streaming (M)
Fixed system block, prefix with tool messages, preamble, compaction turn, memory events, streamed provider through the proxy, `LLM_STREAM` fallback. **Exit:** vLLM prefix-cache metrics show hits from turn 2; a 40-turn session compacts and replays; streamed hermes tool calls verified on vLLM 0.11 or the fallback documented.

### M3 — snapshots, park and resume (M)
Base snapshot with the harness idle before it connects out, and restore from it; per-session park and idle snapshots; resume with log return; workspace manifest at settle; compose devices and the `vm/` cache volume. **Exit:** `act()`→`ready` < 200 ms from snapshot; park and resume a session across an hour with the same `seq`; kill the VM mid bash → resume with `op_state{lost}`; two VMs from one snapshot produce distinct ids; `AGENT_ENGINE` default flips to `harness` for the default tenant.

### M4 — MCP gateway (M)
`mcp-gateway/` tool server, upstreams, policy, pins and quarantine, promoted tools, elicitation relay, gateway events; `gateway_forwarder.py`; `MCPProxySkill` removed; WCC config migrated. **Exit:** a WCC events task completes through the gateway from inside the VM; a changed upstream definition quarantines the pinned session and a new session sees the new tool; `search_tools`/`call_tool` and a promoted tool both work; the gateway restarts mid-session without losing a pin.

### M5 — Temporal, questions, schedules (M)
`temporalio` dependency, `temporal` and `temporal-ui` compose services, `flows/` package: `SessionWorkflow` (signals, queries, timers, continue-as-new), the four activities in `app/host/worker.py`, worker bootstrap, schedules from `tenants.json`, `question` tool and `answer_to`, park/resume, escalation, tickets from `deliver`, workflow tests on the time-skipping test server, the CLI runbook. **Exit:** a task that asks a question, waits 30 minutes with the VM parked, and finishes after the answer; a 10-minute fake MCP tool completes with no client connected and the Temporal UI shows heartbeats then completion; kill agent-api mid-task → the activity retries from its heartbeat `seq` and the task finishes with no duplicate turn; the morning-brief schedule fires.

### M6 — replay hardening, async option, legacy removal (M)
`replay`/`verify` CLI; traces endpoints read from the fold; `migrate.py --events`; guest tenant on the same engine and `guest.py`'s loop deleted; `app/agent/loop.py` removed; `async_tools=True` measured on a fixed task set; section 14 as tests; WCC tenants moved. **Exit:** every golden and CI log replays; async finding recorded; all tenants on `harness`.

Deferred: guard policy ⑦, reviewer stratum, subagents, a second LLM provider through the proxy, embeddings for memory recall, a bucket backend for the sink and the memory store.

---

## 14. Edge cases as a test list

- Same `input_id` twice → one `task` signal acted on, one turn; the second POST attaches.
- `finish_reason = length` → no tool in that message runs.
- Malformed tool `arguments` → one repair turn, then an error result.
- Tool not in `session_start.tools`, or a side mismatch → `guard_decision{deny, allowlist}`; in a log it is a schema error.
- Device result for an unknown or terminal `call_id` → 409, `input{dedupe: late}`.
- Device result after an agent-api restart → accepted, task completes.
- Client disconnects mid-task → task continues under the session workflow; `GET /events?after_seq` catches up.
- MCP tool exceeds upstream `timeout_s` → `op_state{timeout}`, error result, turn continues.
- Upstream definition changes under a pinned session → quarantine error; new session sees the new pin.
- Gateway unreachable → error result; task continues; `gateway{error}` host event.
- vLLM 502/503/504 or down → proxy retries, auto-start path, one `error` event after the last retry.
- Tool result over `spill_threshold` → truncated event, full text on :5001, `bulk` with sha256.
- Context over window − reserve → compaction turn, retry once, then `failed`.
- `max_tools` reached → wrap-up turn without tools, `settle{done}`.
- Question deadline with `ui_attached=false` → park, workflow waits on `answer`; answer → resume; `answer_wait_s` timer → `escalated`.
- Doom loop → question.
- Park requested mid-bash → park waits for the turn boundary; a bash longer than `park_grace_s` is killed and recorded `op_state{killed}`.
- Two VMs restored from one base snapshot → distinct ids and logs.
- VM dies mid-tool → resume via log return, open call `lost`, task completes.
- Session idle past `session_idle_s` → VM snapshotted and destroyed; the next task restores it.
- Activity heartbeat stops (worker crash) → Temporal retries `run_task` once with the last heartbeat `seq`; the guest resumes from it; a second failure fails the task and the workflow stays signalable.
- Session workflow history past 10 000 events or 50 tasks → continue-as-new carries the seen `input_id`s and the parked state.
- Replay mismatch or broken checkpoint chain → hard error naming the seq.
- A hook reading `time()` → purity test fails.

---

## 15. Test strategy

| Layer | How |
|---|---|
| protocol | unit: envelope/ack/resend, schema rules, canonical hashing, id derivation, fold, checkpoint chain, goldens; purity test |
| guest | unit with fakes for transport, provider, ops, clock |
| host | unit; proxy against a fake vLLM; sides against fake skills and a fake device; state sync |
| gateway | unit for policy and pins; integration against a fake upstream and the real WCC server in staging |
| flows | `SessionWorkflow` on the SDK's time-skipping test server with mocked activities: dedupe, park → answer, park → timer, continue-as-new; one end-to-end run against the compose dev server |
| microVM | one job on a `/dev/kvm` node: restore ×2, park/resume, kill/resume |
| determinism | two live runs with the scripted provider → identical state hashes |
| replay | every golden and CI log replays after the run |
| live | one manual checklist per milestone: phone, avatar page, one WCC tenant |

CI: `uv run pytest` for agent-api and the gateway as today; `make image` and the microVM job on the server runner.

---

## 16. Risks

| Risk | Mitigation |
|---|---|
| Streamed hermes tool calls mis-assemble on vLLM 0.11 | D17 flag; the non-streamed path is what runs today. |
| Firecracker inside the agent-api container needs `/dev/kvm`, `/dev/vhost-vsock` and capabilities compose must grant | M0 spike on the actual server; fallback is a sibling `sandbox-host` container the worker talks to over a UDS. |
| Park/resume across snapshots with an open vsock connection | Park only at a turn boundary with no guest operations running; the guest closes its sockets before `parked`. |
| Workflow code must be deterministic; a careless import or clock read breaks replay of the workflow history | Workflow module imports only `flows/` and the SDK; the sandbox rejects the rest; activities do all I/O. |
| Temporal dev server with SQLite is not meant for production | Fine for one home server; the Postgres `auto-setup` compose is the documented step up, same worker code. |
| Gateway pins make upstream deploys visible as quarantines | Quarantine is per pinned session only; new sessions pick up changes; the WCC servers deploy between sessions in practice. |
| Thread-per-session coordinator and pollers inside an asyncio app | Hand-off only through queues and `call_soon_threadsafe`; `SessionQueue`'s semaphore caps concurrent sessions at 5. |
| OpenShell's integration surface differs from section 11's assumptions | Each assumption is a named hook; M0 records which hold and which need an adapter. |
| Three new services (Temporal, gateway, VM image) on one home server | Each has a compose profile and can be disabled; `AGENT_ENGINE=legacy` keeps the phone working without any of them. |

Open questions: (1) does the iOS app adopt `GET /events` and `input_id` in its next release? (2) `answer_wait_s` for a parked phone task — hours or days? (3) which WCC tenant is the first non-owner canary? (4) does the VM image carry a project toolchain (node, python packages) for the default tenant, and who builds it?

---

## 17. Success metrics and the switch-over gate

Per tenant before `engine: harness` is set for it: existing tests green under both engines; 100 % of that tenant's sessions since M1 replay and verify; 0 duplicate turns from client retries; device and MCP completion after a dropped stream or restart ≥ 95 %; `act()`→`ready` < 200 ms p50 from snapshot; p50 non-model time per turn < 300 ms in Firecracker; parked task resume ≥ 95 %; prefix-cache hit rate reported. Order: default tenant → guest → WCC.

---

## 18. Fit to the v4.1 diagram

Coloured boxes map as in draft 5 §16, with the restored parts: Zone 0 → the microVM and `harness/guest/`; Zone 1 → `agent-api/app/host/`; vsock control channel → section 4; LLM egress proxy → `llm_proxy.py` (:5010, no key injection here); gateway forwarder → `gateway_forwarder.py` (:5011); MCP gateway (tool server, upstreams, policy, pins) → section 9; Step Functions stub → Temporal, section 10; actor worker → `worker.py` (act(), heartbeat, artefacts; no git); sandbox manager → `sandbox/firecracker.py` with park/idle snapshots; state sync → `state_sync.py` and the session dir; S3 → `local_dir` sink for the event log, spill and workspace, and the local `data/users/` and session `memory/` files for memory (D23); no bucket anywhere.

Grey boxes: ⑦ in-VM guard → D5; reviewer stratum → D9; audit/review UI → D10. Orange (secret injection), bind_check, token broker, DLP, launcher → OpenShell (section 11).

Diagram follow-ups for v4.2: ports 5000 · 5001 · 5010 · 5011; a fourth tool side "host" beside device and mcp, with `side_result` on the control channel; `park`/`parked` and the per-session snapshot in the sandbox manager box; retitle the Step Functions stub "Temporal: session workflow · signals · schedules"; drop the web proxy; relabel every orange element "OpenShell". Retitle the S3 box "local data/ volume: event log · snapshots · spill · memory files (bucket later)".
