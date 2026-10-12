# agent-api host (`agent-api/app/host/`)

The host half of a session: the Worker that boots, parks and resumes the
guest, the HTTP API both drivers serve, and the agent-api configuration the
harness depends on. The wire is docs/PROTOCOL.md, the Temporal path
docs/FLOWS.md, the host log docs/EVENTS.md "Host log", the gateway
docs/GATEWAY.md. The module docstrings (`worker.py`, `state_sync.py`,
`gateway_forwarder.py`, `sandbox/firecracker.py`, `sandbox/base_snapshot.py`)
carry the details.

Shared definitions come from `protocol` (one home each): the ports
(`protocol.ports`), the limit defaults and `SEGMENT_EVENTS`
(`protocol.limits`; `Worker.limits()` starts from `DEFAULT_LIMITS` and
applies only the `HARNESS_*` env and tenant overrides, whose own defaults are
`DEFAULT_LIMITS` too), atomic writes and file digests (`protocol.fsio`: tickets
and `vm/parked.json` durable, `session.json` and snapshot `META.json` renamed
without fsync as before), `now_ms`/`now_iso`/`elapsed_ms` (`protocol.clock`),
the usage rule (`protocol.usage`), the log return bytes
(`protocol.bulk.encode_log`), the bearer and internal paths (`protocol.mcp`)
and `HARNESS_VERSION` (`protocol.version`). The Temporal defaults
(`TEMPORAL_TASK_QUEUE`, `TEMPORAL_NAMESPACE`, `HARNESS_MAX_QUEUE_WAIT_S`) are
`flows`' constants. Events per segment are a constant, not a knob (the
store finds a seq's segment by it).

## 1. Worker lifecycle

One long-lived guest per session (`Worker`, `vm_state` none → booting →
running → parking → parked → booting ...).

| Step | What the worker does |
|---|---|
| boot | A fresh session restores the BASE snapshot when the sandbox has one, else a rootfs start; `hello{resume: false}`. The guest's `session_start.harness_version` is checked against the host's `HARNESS_VERSION`: a mismatch logs a `harness_skew{guest, host}` host event (warn only). |
| park | Control `park{reason}`, wait for the acked `parked`, close the guest's :5000/:5001/:5011 listeners (held gateway calls keep running), `sandbox.snapshot({session}/vm/snapshot)`, write `vm/parked.json` for a restarted worker. A guest that parks itself on a question is adopted the same way. |
| wake | The next task, `ui_response` or `side_result` of a parked session restores the session snapshot (no hello; `seq`/`cseq` continue) and sends the held controls, `ui_response` first. Fallback: base restore + `hello{resume: true}` + log return. |
| idle | `HARNESS_SESSION_IDLE_S` (1800) after a settle: park, snapshot, destroy. |
| death | Link down and sandbox not alive mid-task: base restore + `hello{resume: true}`; the guest pulls the acked log on :5001 and marks open calls `lost`, and the worker cancels them (a late `side_result` is dropped and logged as an `input{dedupe: late}` host event). `HARNESS_MAX_RESUMES` (1) per task, then the task fails. |

After an agent-api restart, `POST /api/v1/tool-result` for an unknown call id
loads the tenant's parked sessions first: each Worker rebuilds its open device
calls from the fold, so the result is accepted and wakes the guest.

### Session identity and directories

A session is `(user, tenant, session_id)` (`SessionRef`), compared exactly
everywhere: the Worker registry key `{user}/{tenant}/{session_id}` is
injective (`/` is in no safe id), a device call carries its session's
`SessionRef` and `POST /tool-result` resolves it only for that exact user and
tenant, listings and `Workers.load_parked` compare `session.json`'s tenant,
never a name prefix. Tenant names may contain `_`.

Directory: `{SESSIONS_ROOT}/{user}/{tenant}.{session_id}/` (`.` is in no safe
id, so the name is injective). The legacy `{tenant}_{session_id}/` is
ambiguous (tenant `wcc` + session `event_x` = tenant `wcc_event` + session
`x`): it is still read in place, but only for the session its `session.json`
names; a new session never goes there. Rename legacy directories with
`python -m app.session.migrate --layout [--dry-run] [<sessions_root>]`
(agent-api stopped; section 5).

### Task cap, wall clock, tickets

- **Cap.** `Workers` owns one admission (`HARNESS_MAX_CONCURRENT_TASKS`, 5)
  that both drivers go through (`Worker._act`): a task takes a slot (after
  its session's task lock, so a task queued behind its own session holds
  none) before anything is sent to the guest, gives it back at a question
  park and at its end; the wake of a task parked on a question (the answer,
  an abort, `prepare_vm` of the resume) takes it again first. The Temporal
  worker runs at most `2 * cap + 10` activities at once.
- **Wall clock.** One value per task: the tenant's `wall_clock_s`
  (`SessionConfig`, `worker.session_config`), else `HARNESS_WALL_CLOCK_S`
  (600); on the workflow path the chat route puts it in `TaskSignal`.
  It travels in the `task` message (`wall_clock_s`); the guest arms it per
  task and settles `timeout` (hello `limits.wall_clock_s` is only the
  default for a task message without one). The limit counts from when the
  task **starts** on both paths, never the wait for a slot: the host waits
  it + `WALL_CLOCK_GRACE_S` (30, `flows.activities`) as a backstop
  (`Worker._wait`, armed in `_run` after `_act` took the slot, paused while
  parked). On the workflow path `run_task`'s Temporal timeouts are only a
  safety net: start-to-close = schedule-to-close = wall + 30 +
  `HARNESS_MAX_QUEUE_WAIT_S` (3600, `SessionConfig.max_queue_wait_s`: the
  longest wait for Temporal's queue plus a slot), liveness is the heartbeat
  (every `HARNESS_HEARTBEAT_S` from `start_task`, also while the task is
  queued; timeout 120 s).
- **Dropped.** A task that has not started yet (waiting for its session's
  task lock or a slot) whose `run_task` is cancelled or times out, or whose
  input `deliver` ends first, is dropped (`Worker.drop_queued`, host event
  `workflow{kind: dropped, input_id, reason}`): it never runs later and no
  abort goes to a guest. A started one is aborted (`abort_and_wait`).
- **Ticket.** `Worker.finish_input`, where both paths end an input, writes
  `{session}/tickets/{ticket_id}.json` once (`ticket_id =
  derive_key_id("ticket", user, tenant, session_id, input_id)`):
  `{ticket_id, input_id, driver, outcome, answer, question_id, error,
  manifest, usage, completed_at}`; `manifest`, `usage` and `answer` are the
  guest's settle (its manifest is `protocol.bulk.manifest`), else the host's
  failure result.
- **Answers.** `Worker.answer(question_id, text, input_id)` sends
  `ui_response{input_id: "{input_id}:answer:{question_id}"}` for the task of
  `input_id` on both paths (the client's own `input_id` of an answer POST is
  not used), so the guest dedupes a re-sent answer.

### Sandbox (`app/host/sandbox/`)

`start(session_dir)`, `stop()`, `is_alive()`, `restore(session_dir,
snapshot_dir=None)`, `snapshot(snapshot_dir)`, `has_snapshot(snapshot_dir=None)`.

- BASE snapshot: `{HARNESS_VM_CACHE}/base/{vmstate,mem,MANIFEST.json}`
  (default cache `/var/lib/secondbrain/vm`; build and manifest: docs/IMAGE.md).
  Per-session snapshot: `{session}/vm/snapshot/{vmstate,mem,META.json}`. It
  is single-use: after a successful restore `META.json` is renamed
  `META.json.consumed`.
- Firecracker records drive and vsock paths **relative** to its cwd (the
  session's `vm/`), so one base snapshot serves every session (`workspace.img`
  and `../ipc/v.sock` resolve per session).
- `restore` raises `SandboxError` before launching anything when the snapshot
  is missing or was taken from another image or Firecracker version; the
  worker then falls back to `start()`.
- Unix sockets at any path length (`app/host/uds.py`: `bind_unix` for the
  `Endpoints` listeners, `connect_unix` and `firecracker_path` for the
  Firecracker API socket; the connect half is `protocol.uds`, which the
  guest's test connector uses too): a path past sun_path's limit goes
  through `/proc/self/fd/{dirfd}/{name}`.
- The base-snapshot build is `sandbox/base_snapshot.py` (`python -m
  app.host.sandbox.base_snapshot build-base|check-base`; the old
  `python -m app.host.sandbox.firecracker ...` still runs it).
- `LocalSandbox` (tests, no KVM) emulates it: snapshot = SIGSTOP the parked
  guest, restore = SIGCONT it (or start a guest process when none is kept).
  Its guest workspace is the private `{session}/vm/workspace`, so nothing the
  host writes shows up in the guest's walk.

### Tools at session start

`Worker.tool_entries()` = the host/device entries from the skill registry,
then the gateway's `GET /internal/tools` entries verbatim (one named like a
host tool is dropped). The first successful gateway answer is kept for the
session (the gateway pinned it; a resume hello reuses it even when the
gateway is down). A failure is logged and the session starts without MCP
tools; the next boot tries again. MCP calls never reach `sides.py`: the guest
calls the gateway itself over :5011 (docs/PROTOCOL.md section 7).

## 2. HTTP API

| Route | Behaviour |
|---|---|
| `POST /api/v1/chat {message, session_id, input_id?, answer_to?}` | One input. The path is configuration: `TEMPORAL_ADDRESS` set → always the session workflow (`signal_with_start` with a `task` signal); while Temporal is connecting or down the request waits `TEMPORAL_READY_WAIT_S` (5), then 503 with `Retry-After` (`TEMPORAL_RETRY_AFTER_S`); a supervisor reconnects and restarts the Temporal worker every `TEMPORAL_RECONNECT_S`. Unset → always the direct path (`Worker.act`). A session never switches path. Unsafe `session_id` → 400. The SSE response streams the Worker's events for that `input_id` until `done`; it is a projection, and a client disconnect never affects the task. A redelivered `input_id` attaches to the running task or replays the stored answer. |
| `POST /api/v1/chat {answer_to: question_id, message}` | Workflow parked on that question → `answer` signal; otherwise `ui_response` to the live or parked guest. Either way the response streams the task's continuation. No task waits on that question → the answer "No question is waiting for this answer." and an error. |
| `POST /api/v1/sessions/{id}/abort` | Workflow path: the `abort` signal (404 when the session has no workflow; 503 while Temporal is not ready). Direct path: `abort{reason: "aborted"}` to the live worker's guest (409 when no task runs). |
| `GET /api/v1/sessions/{id}/events?after_seq=N` | The current run's guest events with `seq > N` (JSON list), for a client catching up after a disconnect. |
| `GET /api/v1/sessions/{id}` | `{session_id, updated_at, workflow}`; `workflow` is the workflow's `status` query, or `null`. |
| `GET /api/v1/sessions/{id}/traces`, `GET /api/v1/sessions/traces/recent` | Trace rows (section 3). |
| `POST /api/v1/tool-result {tool_call_id, result}` | A device result. Unknown or terminal id (or another tenant's) → 409; not deliverable → 503, retry. |
| `POST /api/v1/guest/chat` | The guest avatar page: the same harness chat path with the `guest` tenant (section 4); the page sees only SSE `token`, `avatar_command` and `done`. |
| `POST /internal/gateway-events` | The gateway's events into the host log (docs/GATEWAY.md "Events"). |

A stream that attaches gets the open device calls (`tool_call`) and the open
question (`question{question_id, text, options, deadline}`) re-emitted, so the
phone gets them on reconnect.

**Question park on the direct path.** With `answer_wait_s > 0` the input's
stream stays open while the guest is parked on a question (the task's
admission slot is released meanwhile). `POST /chat {answer_to}` sends `ui_response` and the
guest wakes and continues; an `answer_to` for another question gets the
no-question error. When `answer_wait_s` runs out the worker sends
`abort{reason: "escalated"}` (host event `workflow{kind: answer_timeout}`) so
the guest writes the escalated settle; the stream ends with `error{outcome}`
and `done`, as at `deliver`.

## 3. Traces from the event log

`GET /sessions/{id}/traces` and `GET /sessions/traces/recent` answer
`{"traces": [...]}` with rows `{ts, event, detail, duration_ms?}`, built from
the event log (`app/session/eventlog.py`; the mapping per event type is in
its docstring). `history.jsonl` and `logs/trace.jsonl` are no longer written.
The host log is never read for traces. `/{id}/traces` reads runs newest
first and stops once it has `limit` rows or reaches back before `since`;
`/traces/recent` skips a run whose segments were last written before its
cutoff, so an idle session costs a stat. Migrated sessions (section 5) are
read the same way.

## 4. Tenants (`data/tenants.json`)

Read once at startup; restart to apply.

- Every tenant runs the harness engine. The harness needs the directory
  session store: `SESSION_BACKEND` other than `dir` stops agent-api at
  startup (`app/session/factory.py`).
- Retired keys are accepted, dropped and logged: `engine`, `mcp_servers` and
  `promoted` (the gateway's `gateway.json` owns MCP access; build it with
  `migrate_config`, docs/GATEWAY.md). A tenant listing `mcp_servers` without
  `local_skills` gets `local_skills: []`. The top-level `mcp_servers` map is
  ignored.
- A `default` tenant keyed by `API_SECRET_KEY` is synthesized when no entry
  has that key.
- The guest avatar profile is the tenant `guest` with `auth: "origin"` (no
  key; the browser Origin must be one of its `origins`), synthesized when the
  file has none. `auth: origin` is refused for any other tenant, and a
  `guest` entry must use it. Its origins are part of CORS.
- Session keys (`flows.SESSION_CONFIG_KEYS`, read through `SessionConfig`
  on both paths): `wall_clock_s` (every task's wall clock; unset →
  `HARNESS_WALL_CLOCK_S`), `answer_wait_s`, `max_tasks_per_run`,
  `max_history_events`; and `schedules` (docs/FLOWS.md "Per-tenant config").
- `async_tools` (bool): `hello.config.async_tools` of the tenant's sessions
  (docs/ASYNC-TOOLS.md); unset → `HARNESS_ASYNC_TOOLS` (default false).

## 5. Legacy sessions → event logs (`app/session/migrate.py`)

```
python -m app.session.migrate --layout [--dry-run] [<sessions_root>]   # {tenant}_{sid} -> {tenant}.{sid}
python -m app.session.migrate --events [--dry-run] <sessions_root>
python -m app.session.migrate [--db data/conversations.db] [--root data/sessions]   # sqlite rows
```

`--events` gives every session with a non-empty `history.jsonl` and no run
under `events/` one run `events/r0000`; the sqlite step builds the same run
straight from its rows. A run holds:

- `session_start` with the session's user/tenant, `resumed: false`,
  `migrated: true` and `tools` = the legacy tools the history names (side
  `host`; `[]` when none: the fold's allowlist needs them);
- per user message (line L): `input{task, input_id: derive_key_id("input",
  user, tenant, session_id, "L")}`;
- per legacy tool call: `clock{turn_clock}` + `turn_start` + `llm_response`
  (the assistant tool_calls message) + `tool_call{side: host}` + `op_state`
  (when the duration is known) + `tool_result`; tool calls come from the
  history or else from `logs/trace.jsonl`;
- per assistant reply: `clock{turn_clock}` + `turn_start{regular}` +
  `llm_response{message: {role: assistant, content}}`;
- per user message a `settle{outcome: done, answer: the last reply}`
  (`failed` with an empty answer when it never got one);
- a closing `checkpoint` chained to `session_start`.

Timestamps become `clock.at` and the envelope `ts` (data only). Every run is
checked with `fold.replay` before it is written, through a temporary
directory renamed into place (`state_sync.replace_run_dir`), so a session
has its whole run or none. Idempotent: a session with a run under `events/`
is skipped. `--dry-run` reports what would be written. The legacy files are
left in place.

A migrated session continues under the harness: the worker's first boot of
a migrated run that no guest has continued is a resume (log return), so the
prior conversation is in context. Before it, `StateSync.retool_migrated`
re-pins that run's `session_start.tools` to the hello's effective tools plus
the legacy ones and recomputes the closing checkpoint
(`protocol.fold.checkpoint_body`), so the resumed guest can call every tool
it offers.
