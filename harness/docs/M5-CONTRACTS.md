# M5 contracts — Temporal, questions, schedules

Fixed before the M5 work split across three workstreams (flows + compose,
host, guest). PLAN.md D11, D12, section 10 is the design; M0 already built
`flows/` (SessionWorkflow, ScheduleTickWorkflow, stub activities, schedules,
worker bootstrap) and its payload dataclasses in `flows/activities.py` are the
activity contract below — change them only additively.

## 1. When Temporal drives a session

`TEMPORAL_ADDRESS` set (compose: `temporal:7233`, namespace
`TEMPORAL_NAMESPACE`, default `secondbrain`) **and** the tenant runs on
`engine: harness` → `POST /chat` goes through the session workflow. Unset → the
M1–M4 direct path (`Worker.act`) stays, unchanged, so tests and a Temporal-less
box keep working. agent-api depends on `flows` (editable path dep `../flows`)
and starts the Temporal worker on its own loop at startup (D22,
`flows.worker.build_worker(client, activities=[...real bodies...])`), and calls
`flows.schedules.ensure_schedules` with the loaded tenants doc.

## 2. Activities (host, `agent-api/app/host/activities.py`)

Real bodies with the exact names and payloads of `flows/activities.py`:

| Activity | Body |
|---|---|
| `prepare_vm(PrepareVM)` | `Workers.get_or_create` for the session (tenant looked up by name in the registry); boot/restore the guest so it is ready (fresh: base restore or rootfs; `resume_from_park`: wake the parked snapshot). Idempotent. |
| `run_task(RunTask) -> RunResult` | Not resume: `Worker.act({input_id, text, kind})` (a redelivered `input_id` attaches to the running task or returns its stored settle — already true). Resume: send `ui_response{question_id, answer}` and wait for that task's settle or park. **Heartbeat** every 60 s (and at start) with details `{"last_seq": <acked guest seq>}`. On attempt > 1, read `activity.info().heartbeat_details` as a floor only (M0 spike caveat) and re-attach by `input_id` — never start a second turn. Returns `RunResult(outcome, question_id, last_seq)`: `done/failed/timeout/escalated` from the guest's `settle`, or `parked` + `question_id` when the guest parked for a question (§4). Activity cancellation (workflow `abort`) → send `abort{reason: "aborted"}` to the guest, wait for its settle, re-raise. |
| `park_vm(ParkVM)` | Ensure the session is parked and snapshotted (the guest already parked itself, §4: the worker adopted it; idempotent). |
| `deliver(Deliver)` | Write the ticket projection `{session}/tickets/{ticket_id}.json` = `{ticket_id, input_id, outcome, answer, question_id, manifest, usage, settled_seq}` (ticket_id = `"ticket_" + hash_value(["ticket", user, tenant, session_id, input_id])[:24]` (protocol.canonical; per input, not per settle seq: seqs restart with every guest run); for outcomes with no guest settle — `escalated` from the workflow timer, `aborted` while parked — first wake the guest and send `abort{reason: <outcome>}` so the guest writes the `settle` itself, §4); emit SSE `done` (and `error` for non-done) to streams attached to that `input_id`; host event `workflow{kind: delivered, input_id, outcome}`. Idempotent by `input_id`. |

Host events `workflow{kind: task_signal | activity_start | heartbeat | completed | parked | delivered, ...}` as PLAN 5.

## 3. API (host)

- `POST /chat` (Temporal path): `signal_with_start` `SessionWorkflow` id
  `{user}/{tenant}/{session_id}` with `task` signal `{input_id, text}` and
  start arg `SessionState(config=SessionConfig.from_tenant(entry))`, then stream
  the worker's live events for that `input_id` (attach sink) until `done`. The
  SSE stream is a projection: a client disconnect never affects the task.
- `POST /chat {answer_to: question_id, message}`: if the workflow `status`
  query says `parked` on that question → `answer` signal; else deliver
  `ui_response` directly to the live worker (today's path). Either way the
  response streams the task's continuation.
- `POST /sessions/{id}/abort` → `abort` signal.
- `GET /sessions/{id}/events?after_seq=N` → the session's guest events with
  `seq > N` (JSON list), for clients catching up after a disconnect.
- `GET /sessions/{id}` adds `workflow: <status query result> | null`.
- An open question is re-emitted as SSE `question{question_id, text, options,
  deadline}` to a stream that attaches (like open device calls), so the phone
  gets it on reconnect.

## 4. Guest: questions park the task (D12)

New hello limit `answer_wait_s` (int, default 0; the worker sends the
tenant's `answer_wait_s` only on the Temporal path). On a question deadline
(`clock{question_deadline}`):

- `answer_wait_s > 0` → no deny: the guest **parks itself** with
  `parked{checkpoint_seq, reason: "question", question_id}` (new optional body
  key `question_id`), using the M3 park path (checkpoint, wait acked, idle).
  The question stays open in the state. The worker sees the acked `parked`
  with reason `question`, snapshots (adopt path), and `run_task` returns
  `parked` + `question_id`.
- `answer_wait_s == 0` → today's behaviour (deny unanswered, three → escalated).

After resume a `ui_response` for that question is accepted as usual (input
event, question closed, the model woken). `abort{reason}` with reason
`escalated` → `settle{outcome: "escalated"}`; any other reason →
`settle{outcome: "failed", answer: "aborted: <reason>"}` (the settle outcome
enum stays `done/failed/timeout/escalated`; the workflow's own `aborted`
outcome maps onto it).

Input-required MCP questions (M4) follow the same rule: deadline with
`answer_wait_s > 0` parks instead of cancelling.

## 5. flows + compose

- `SessionConfig.from_tenant` reads `wall_clock_s`, `answer_wait_s`,
  `max_tasks_per_run` (exists). Tenants example gets `answer_wait_s` (phone
  default 86400) and a `schedules` example on the default tenant
  (`[{cron: "0 7 * * *", prompt: "Morning brief", session: "daily"}]`).
- Compose `temporal` service: `temporalio/temporal` CLI image running
  `server start-dev --ip 0.0.0.0 --db-filename /data/temporal.db --namespace
  secondbrain`, data volume, gRPC 7233 on the internal network only, UI 8233
  bound to `127.0.0.1` only; agent-api gets `TEMPORAL_ADDRESS=temporal:7233`
  and `depends_on` it (not required to be healthy: the direct path still
  works). Retention 30 days on the namespace (`docs/FLOWS.md` says how).
- `harness/docs/FLOWS.md`: the CLI runbook (list, describe, signal,
  terminate, reset, schedules) and `RUNBOOK-M5.md` for the four M5 exit
  criteria on the server.
