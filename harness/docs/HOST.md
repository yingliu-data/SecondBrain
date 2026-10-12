# agent-api host (`agent-api/app/host/`)

The host half of a session: the Worker that boots, parks and resumes the
guest, the HTTP API both drivers serve, and the agent-api configuration the
harness depends on. The wire is docs/PROTOCOL.md, the Temporal path
docs/FLOWS.md, the host log docs/EVENTS.md "Host log", the gateway
docs/GATEWAY.md. The module docstrings (`worker.py`, `state_sync.py`,
`gateway_forwarder.py`, `sandbox/firecracker.py`) carry the details.

## 1. Worker lifecycle

One long-lived guest per session (`Worker`, `vm_state` none → booting →
running → parking → parked → booting ...).

| Step | What the worker does |
|---|---|
| boot | A fresh session restores the BASE snapshot when the sandbox has one, else a rootfs start; `hello{resume: false}`. |
| park | Control `park{reason}`, wait for the acked `parked`, close the guest's :5000/:5001/:5011 listeners (held gateway calls keep running), `sandbox.snapshot({session}/vm/snapshot)`, write `vm/parked.json` for a restarted worker. A guest that parks itself on a question is adopted the same way. |
| wake | The next task, `ui_response` or `side_result` of a parked session restores the session snapshot (no hello; `seq`/`cseq` continue) and sends the held controls, `ui_response` first. Fallback: base restore + `hello{resume: true}` + log return. |
| idle | `HARNESS_SESSION_IDLE_S` (1800) after a settle: park, snapshot, destroy. |
| death | Link down and sandbox not alive mid-task: base restore + `hello{resume: true}`; the guest pulls the acked log on :5001 and marks open calls `lost`, and the worker cancels them (a late `side_result` is dropped and logged as an `input{dedupe: late}` host event). `HARNESS_MAX_RESUMES` (1) per task, then the task fails. |

After an agent-api restart, `POST /api/v1/tool-result` for an unknown call id
loads the tenant's parked sessions first: each Worker rebuilds its open device
calls from the fold, so the result is accepted and wakes the guest.

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
| `POST /api/v1/chat {message, session_id, input_id?, answer_to?}` | One input. Temporal up: `signal_with_start` of the session workflow with a `task` signal; else the direct path (`Worker.act`). The SSE response streams the Worker's events for that `input_id` until `done`; it is a projection, and a client disconnect never affects the task. A redelivered `input_id` attaches to the running task or replays the stored answer. |
| `POST /api/v1/chat {answer_to: question_id, message}` | Workflow parked on that question → `answer` signal; otherwise `ui_response` to the live or parked guest. Either way the response streams the task's continuation. No task waits on that question → the answer "No question is waiting for this answer." and an error. |
| `POST /api/v1/sessions/{id}/abort` | Temporal: the `abort` signal (404 when the session has no workflow). Direct: `abort{reason: "aborted"}` to the live worker's guest (409 when no task runs). |
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
stream stays open while the guest is parked on a question (the global slot is
released meanwhile). `POST /chat {answer_to}` sends `ui_response` and the
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
- Session workflow keys: `wall_clock_s`, `answer_wait_s`,
  `max_tasks_per_run`, `max_history_events`, `schedules` (docs/FLOWS.md
  "Per-tenant config").

## 5. Legacy sessions → event logs (`app/session/migrate.py`)

```
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
