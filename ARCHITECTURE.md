# SecondBrain architecture

SecondBrain's backend runs one AI agent session per user conversation. Each
session's agent loop runs inside its own **Firecracker microVM** (the
*guest*); the **host** (agent-api) owns everything trusted — credentials,
storage, the LLM, tools that need secrets, and the phone. Every decision the
guest makes is written to an **event log**, so a session can be replayed,
verified, parked to disk and resumed exactly.

This file is the map. The detailed specs live in [harness/docs/](harness/docs/);
each section below links to the one that owns the topic.

## 1. The big picture

```text
 iPhone app · avatar page
        │  HTTPS + SSE  (Cloudflare tunnel)
        ▼
 ┌────────────────────────── agent-api (FastAPI, one container) ──────────────────────────┐
 │ routes: /api/v1/{chat, sessions, tool-result, guest/chat, skills} · /health            │
 │ tenants · auth · skills (host-side tools) · session store (data/sessions)              │
 │                                                                                        │
 │ HOST HALF, per session:  Worker ── vsock endpoints (:5000 control, :5001 bulk)         │
 │                                  ── LLM proxy (:5010) ── gateway forwarder (:5011)     │
 │                                  ── StateSync (durable event log) ── sandbox manager   │
 │ Temporal worker (activities)  ·  group-commit writer  ·  admission (concurrency cap)   │
 └─────────┬─────────────────────────┬───────────────────────────┬────────────────────────┘
           │ vsock (Unix sockets)    │ HTTP                      │ gRPC
           ▼                         ▼                           ▼
   microVM per session        mcp-gateway container        temporal (dev server)
   GUEST HARNESS              policy · pins · upstreams    session workflows · schedules
   stdlib Python, event-      ──▶ WCC MCP servers, …
   sourced; /workspace drive
           │
           └── LLM requests via the host proxy ──▶ vLLM (secondbrain-llm, Qwen3-14B)
```

Four deployable pieces, all in this repo:

| Piece | Where | Runs as |
|---|---|---|
| Guest harness | [harness/guest/](harness/guest/) + [harness/protocol/](harness/protocol/) | `guest.pyz` inside each microVM (built by [harness/image/](harness/image/)) |
| Host / API | [agent-api/](agent-api/) | the `agent-api` container |
| MCP gateway | [mcp-gateway/](mcp-gateway/) | the `mcp-gateway` container |
| Workflows | [flows/](flows/) | imported by agent-api (Temporal worker in-process) |

The four are members of **one uv workspace** (root `pyproject.toml`, one
`uv.lock`). `harness/protocol` is shared by the guest, agent-api and the
gateway; `flows` is used only by agent-api.

[index-mcp/](index-mcp/) is not a member: it is a standalone uv project with
its own `uv.lock`, an MCP server for code search over a MongoDB Atlas store
that runs on the host and is an upstream of mcp-gateway (see its README).

## 2. Hard rules (the design depends on them)

1. **The guest is plain standard-library Python** and imports nothing from the
   other packages — it is packed into the VM image. `harness/tests/test_purity.py`
   enforces it.
2. **The guest never listens.** It connects out to the host over vsock; the host
   cannot reach into the VM.
3. **The guest holds no secrets.** No API keys, no upstream tokens, no network
   device. LLM calls, MCP calls and secret-needing tools all go through the host.
4. **Every decision is an event; state = fold(events).** Replay never calls the
   LLM or runs a tool. The committed prompt prefix is never rewritten.
5. **The host is the canonical store.** The guest's log is a buffer; an event is
   durable only once the host has written it to disk and acked it.
6. **Ids are derived, never random** (`protocol.canonical.derive_id`), so two VMs
   restored from one snapshot behave deterministically.

## 3. One request, end to end

1. **Client → agent-api.** `POST /api/v1/chat` (HMAC-signed) with an `input_id`.
   Redelivered input ids are deduplicated.
2. **Driver.** If `TEMPORAL_ADDRESS` is set, the route signals the session's
   Temporal workflow (`flows/session_workflow.py`); otherwise the *direct* driver
   runs the task in-process. Both drive the same `Worker` API. The choice is
   configuration — a session never switches driver.
3. **Admission.** The task takes a slot (`HARNESS_MAX_CONCURRENT_TASKS`) before
   any VM work; a task parked on a question gives its slot back.
4. **VM.** The worker restores the session's snapshot, else the shared base
   snapshot (guest idle, all modules preloaded), else cold-boots the rootfs.
   The guest connects out, the host sends `hello` (config, tools, limits).
5. **Guest loop.** The coordinator sends the task to the model through the LLM
   proxy (streamed tokens go to the client as SSE), runs tool calls, wakes the
   model on results, and repeats until it settles (`done`, `failed`, `timeout`,
   `escalated`, `aborted`).
6. **Events.** Every guest event goes host-ward on `:5000`; the host appends it
   to `{session}/events/rNNNN/*.jsonl` through the group-commit writer, then acks.
7. **Finish.** One ticket is written per input; the stream ends with `done`
   (and `error` first if the task did not succeed). The VM stays up until the
   idle timer parks or destroys it.

## 4. Tool sides

Each tool has a side, fixed at session start ([PROTOCOL.md](harness/docs/PROTOCOL.md)):

| Side | Runs | Examples |
|---|---|---|
| `guest` | inside the VM, by the guest's operation manager | `bash`, `read`, `write`, `edit`, `grep`, `glob`, `ls`, `todo`, `question` |
| `host` | in agent-api (`app/skills/`), secrets stay on the host | `web_search`, `email`, `github_cli`, `remember`, `avatar_control` |
| `device` | on the iPhone, via SSE `tool_call` + `POST /tool-result` | `calendar`, `reminders`, `contacts`, `clipboard` |
| `mcp` | on an upstream MCP server, through the gateway | `wcc-events__draft_event`, `search_tools`, `call_tool` |

The guest treats all four the same way: a `tool_call` event, an operation with a
timeout, a `tool_result` event.

## 5. The guest — `harness/guest/` ([GUEST.md](harness/docs/GUEST.md))

| Module | Role |
|---|---|
| `main.py` | entry point: preload modules, idle-wait (the snapshot point), connect, hello, run |
| `transport.py` | the `:5000` control channel: JSONL, acks, resend on reconnect, park on ack |
| `coordinator.py` | the single-writer loop: turns, tool calls, wake rules, limits |
| `taskview.py` | one reducer for task bookkeeping, used by both live handling and restore |
| `questions.py` · `park.py` · `mcp_ops.py` · `restore.py` | question deadlines and question parks · park/settle/manifest · MCP call lifecycle · fold-and-resume after a restart |
| `context.py` | prompt assembly: fixed system block + committed prefix + per-turn preamble |
| `provider.py` | OpenAI-style chat completions over `:5010`, streamed |
| `ops.py` | operation manager: thread pool for file tools, process groups for bash, spill files |
| `mcp.py` | MCP client over `:5011` (input-required relay, re-attach after a park) |
| `bulk.py` · `eventlog.py` | `:5001` uploads and log return · the guest's local event buffer (batched writes) |

**Async mode** (`async_tools`, per tenant or `HARNESS_ASYNC_TOOLS`): the model is
woken per tool result instead of after all of them, running calls show a
`[pending]` placeholder, and late results are appended without rewriting the
prefix ([ASYNC-TOOLS.md](harness/docs/ASYNC-TOOLS.md)).

## 6. The host — `agent-api/app/host/` ([HOST.md](harness/docs/HOST.md))

| Module | Role |
|---|---|
| `worker.py` | one `Worker` per session: task API (`start_task`, `answer`, `abort`, `finish_input`), driver policy; `Workers` registry with idle eviction |
| `vm.py` | VM lifecycle mixin: boot, restore, park, wake, death resume, `vm/parked.json` |
| `hello.py` · `settings.py` · `outcomes.py` | hello assembly · `WorkerSettings` · settle/ticket helpers |
| `state_sync.py` · `durable.py` | the canonical event log and host log · one process-wide group-commit writer (ack only after fsync) |
| `endpoints.py` · `uds.py` · `bulk_server.py` | per-session vsock listeners · long-path Unix sockets · `:5001` server |
| `llm_proxy.py` · `gateway_forwarder.py` · `http_clients.py` | `:5010` → the LLM vendor (key, path and body rules; budgets, usage) · `:5011` → mcp-gateway (identity headers, held calls) · shared HTTP clients |
| `sides.py` · `adopted.py` | host and device tool execution · device calls adopted after a restart |
| `admission.py` | the concurrency cap shared by both drivers |
| `activities.py` · `temporal.py` | Temporal activity bodies · Temporal connection supervisor |
| `stream.py` · `sanitize.py` | events → SSE for clients · tool-result sanitising |
| `sandbox/` | `firecracker.py` (microVMs, snapshots), `local.py` (guest as a subprocess, for tests and no-KVM boxes), `base_snapshot.py` (build the shared base snapshot) |

Restart safety: a new process that finds an unfinished input resumes that run
(log return + `hello{resume}`) instead of starting over; open device calls of a
parked session are re-adopted from the log; shutdown parks running guests.

## 7. The protocol — `harness/protocol/` ([PROTOCOL.md](harness/docs/PROTOCOL.md), [EVENTS.md](harness/docs/EVENTS.md), [REPLAY.md](harness/docs/REPLAY.md))

| Module | Role |
|---|---|
| `messages.py` · `ports.py` | control-channel envelopes, acks, resend · the vsock port map |
| `events.py` · `fold.py` | event schema and validation · the pure fold (state, checkpoints, index fields) |
| `replay.py` | on-disk layout, `replay` / `verify` CLI, cross-run session index |
| `canonical.py` | canonical JSON, hashing, derived ids |
| `bulk.py` · `mcp.py` · `sse.py` | bulk framing and log codec · MCP wire constants, identity headers, bearer · SSE grammar |
| `limits.py` · `version.py` · `usage.py` · `clock.py` · `fsio.py` · `uds.py` · `allowlist.py` | shared defaults and helpers used on both sides |

Verify any session: `cd harness && uv run python -m protocol.replay verify <session_dir>`.

## 8. MCP gateway — `mcp-gateway/` ([GATEWAY.md](harness/docs/GATEWAY.md))

One MCP server for all guests, in front of every upstream MCP server. Guests see
a small surface (`search_tools`, `describe_tool`, `call_tool` plus promoted
tools). The gateway applies per-tenant policy (`gateway.json`, deny wins),
pins each tool definition per session (a changed upstream definition is
quarantined for sessions that pinned the old one), relays input requests from
upstreams, and posts events back to agent-api's host log.

## 9. Temporal — `flows/` ([FLOWS.md](harness/docs/FLOWS.md))

One long-lived `SessionWorkflow` per session (`{user}/{tenant}/{session_id}`):
task and answer signals, a parked question waits on a durable timer
(`answer_wait_s`, then escalation), continue-as-new on long histories.
Activities (`prepare_vm`, `run_task`, `park_vm`, `deliver`) are implemented in
agent-api. Schedules (e.g. a morning brief) come from `tenants.json`.

## 10. Storage

```text
data/sessions/{user}/{tenant}.{session_id}/
    session.json                 metadata
    events/r0000/000001.jsonl    guest event log, one directory per guest run (canonical)
    events/host-000001.jsonl     host log: workflow, vm, usage, gateway, side calls
    tickets/                     one per input
    workspace/                   files pulled from the VM at settle · manifest.json
    vm/                          workspace.img · snapshot/ · parked.json
    memory/                      session-tier memory
data/users/{user}/               profile, user-tier memory (MEMORY.md + files)
```

Old `{tenant}_{session_id}` folders are ignored (a startup warning names them);
`python -m app.session.migrate --purge-legacy-layout` deletes them.

## 11. Configuration

| Source | Holds |
|---|---|
| `agent-api/data/tenants.json` | tenants, API keys, prompts, local skills, limits (`wall_clock_s`, `answer_wait_s`, `async_tools`, …), schedules |
| `mcp-gateway/data/gateway.json` | upstream MCP servers and per-tenant tool policy |
| `agent-api/data/config.json` | the LLM vendor (`llm`: vendor, model, url, the key's env var name) and any setting below by its env name (`settings`); an env var wins |
| environment (`agent-api/app/config.py`) | `HARNESS_*` defaults, `TEMPORAL_ADDRESS`, `MCP_GATEWAY_URL`, `INTERNAL_TOKEN`, sandbox paths |
| `hello` | the per-run snapshot of all of the above that the guest sees |

## 12. Deployment and testing

- **Compose** ([docker-compose.yml](docker-compose.yml)): `llm` (vLLM), `agent-api`,
  `mcp-gateway`, `temporal`, `cloudflared`, `dcgm-exporter`. agent-api needs
  `/dev/kvm` for Firecracker (compose sets `HARNESS_SANDBOX=firecracker`; the
  code default is `local`). On a box without KVM set `HARNESS_SANDBOX=local` and
  use the override described in the compose file.
- **Images** build from the repo root (`agent-api/Dockerfile`, `mcp-gateway/Dockerfile`);
  the guest image (rootfs, kernel, base snapshot) is built by
  [harness/image/](harness/image/) ([IMAGE.md](harness/docs/IMAGE.md)).
- **Tests:** `uv sync --all-packages`, then `uv run pytest -n auto` in each of
  `harness/`, `flows/`, `agent-api/`, `mcp-gateway/`. CI runs them as a matrix
  ([.github/workflows/test.yml](.github/workflows/test.yml)). Every event log a
  test writes is replayed and verified automatically.
- **Server checks:** the runbooks [RUNBOOK-M1](harness/docs/RUNBOOK-M1.md) …
  [RUNBOOK-M6](harness/docs/RUNBOOK-M6.md) cover the steps that need KVM and the real model.

## 13. Where to read next

| Topic | Doc |
|---|---|
| Wire protocol, channels, park/resume | [PROTOCOL.md](harness/docs/PROTOCOL.md) |
| Event schema and host log | [EVENTS.md](harness/docs/EVENTS.md) |
| Replay rules, verify CLI, edge-case map | [REPLAY.md](harness/docs/REPLAY.md) |
| Guest internals | [GUEST.md](harness/docs/GUEST.md) |
| Host internals, API, tenants, migration | [HOST.md](harness/docs/HOST.md) |
| MCP gateway | [GATEWAY.md](harness/docs/GATEWAY.md) |
| Temporal | [FLOWS.md](harness/docs/FLOWS.md) |
| VM image and snapshots | [IMAGE.md](harness/docs/IMAGE.md) |
| Async tool calls | [ASYNC-TOOLS.md](harness/docs/ASYNC-TOOLS.md) |
| Original design and milestones | [PLAN.md](PLAN.md) |
