# M6 contracts — replay hardening, async option, legacy removal

Fixed before the M6 work split across three workstreams (replay + bench in
`harness/`, legacy removal in agent-api + gateway, traces + migration in
agent-api). PLAN.md section 13 (M6) is the scope; exit: every golden and CI
log replays, the async finding is recorded, all tenants run on `harness`.

## 1. Replay / verify CLI (`harness/protocol/replay.py`, stdlib)

```
python -m protocol.replay replay <target>...      # fold, print state summary + state_hash
python -m protocol.replay verify <target>...      # validate + fold + checkpoint chain
python -m protocol.replay verify --all <sessions_root>
```

A target is a session directory (reads `events/r*/` runs in order, each run's
segments in order) or a single `.jsonl` file. `verify` exits 0 when every
target passes, else 1 and prints `<target>: seq <n>: <reason>` per failure
(hard error naming the seq, PLAN 14). JSON output with `--json`. The session
directory layout is read from the files alone (no agent-api import). Public
functions `load_session(dir) -> list[list[event]]` (runs) and
`verify_session(dir) -> list[str]` (problems) for agent-api to reuse.

**CI logs:** every event log a test writes (e2e, M3–M5 tests, agent-api host
tests) is verified at the end of its test by a shared fixture/hook in each
suite (`verify_session` on every session dir the test created). A failure
fails the test.

## 2. Traces from the fold (agent-api)

`GET /sessions/{id}/traces` and `GET /sessions/traces/recent` keep their
response shape (`{"traces": [...]}`, rows `{ts, event, detail, duration_ms?}`)
but are built from the event log (tool_call / op_state / tool_result /
turn_start / llm_response / question / settle → rows) for harness sessions.
`logs/trace.jsonl` and `history.jsonl` stop being written (the "kept one
release" projections retire); readers that still need history get it from
the fold. Migrated sessions (§4) are covered the same way.

## 3. Legacy removal (agent-api, gateway)

- `AGENT_ENGINE`, `Tenant.engine` and `_legacy_turn` are removed; an `engine`
  key in tenants.json is accepted and ignored with a warning. Every tenant
  runs on the harness engine. The harness requires the directory session
  store: a non-dir `SESSION_BACKEND` is a startup error with a clear message.
- `app/agent/` is deleted. Shared pieces move first: `SSE_HEADERS` →
  `app/host/stream.py`; `_AVATAR_TOOLS` → the avatar_control skill package;
  `sanitize` → `app/host/sanitize.py`; `tool_result_events` / `tool_results`
  → wherever their remaining callers need them (or deleted if only the
  legacy path used them). `LLMProvider` wiring in main.py goes.
- `routes/guest.py`'s own loop is deleted: the guest avatar page goes through
  the same harness chat path with a guest tenant profile (`auth: origin`,
  `local_skills: ["avatar_control"]`, its limits as today). Wire behaviour of
  the guest route (SSE `token` / `avatar_command` / `done`) is unchanged.
- `GatewayUpstreamSkill` (legacy-only) and agent-api's `app/gateway/
  upstream_skill.py` are deleted; the gateway's legacy routes
  `/internal/upstreams/{name}/tools` and `/internal/call` are deleted with
  their tests. `tenants.json` `mcp_servers` (top-level map and tenant list)
  is accepted and ignored by agent-api (the gateway owns it).

## 4. Legacy sessions → event logs (`migrate.py --events`)

`python -m app.session.migrate --events [--dry-run] <sessions_root>` converts
every session that has `history.jsonl` and no `events/` into an event log run
`r0000`:

- `session_start` with the session's user/tenant, `resumed: false`,
  `tools` = the legacy tools its tool calls name (side host; empty when
  none: the fold's allowlist needs them), and `migrated: true` (new
  optional body key);
- per legacy user message: `input{kind: task, input_id: derive_key_id(
  "input", user, tenant, session_id, <line no>)}`; per assistant reply:
  `turn_start{type: regular}` + `llm_response{message: {role: assistant,
  content}}` + `settle{outcome: done, answer}`; tool calls in the legacy
  history become `tool_call` + `tool_result` with `side: host` when present;
- `clock` events derived from the history's timestamps (data only).

The result must pass `verify_session`. A migrated session continues under the
harness engine: the guest folds the migrated run on its first hello like any
resume, so the prior conversation is in context. (As built: the worker's
first boot of a run that is migrated and not yet continued is a resume, and
`StateSync.retool_migrated` first re-pins that run's `session_start.tools` to
the hello's effective tools plus the legacy ones and recomputes its closing
checkpoint, so the resumed guest can call every tool it offers.) `--dry-run` reports what
would be written. Idempotent: a session with `events/` is skipped.

## 5. Async tool calls (`harness/bench/`)

`python -m bench.async_tools --llm-url URL --model M --tasks harness/bench/
tasks.json [--runs N]` runs a fixed task set through the real guest (as a
subprocess against the fake host, as the e2e tests do) twice per task —
`async_tools` off and on — and prints per task and in total: pass/fail (each
task has a checker), turns, prompt/completion tokens, tool calls, wall time.
A `--fake` mode scripts the LLM for CI. The finding (server, vLLM Qwen3-14B)
goes into `harness/docs/ASYNC-TOOLS.md`; the default stays `async_tools =
False` unless the finding says otherwise (D6).

## 6. Edge cases as tests (PLAN 14)

Every bullet of PLAN.md section 14 maps to at least one named test; the
mapping table lives in `harness/docs/REPLAY.md` (test path per bullet).
Bullets already covered are mapped, missing ones are added in the suite that
owns the behaviour.
