# Temporal control plane (`flows/`)

PLAN.md D11, D12, D22 and section 10 are the design; docs/HOST.md has the
HTTP API both paths serve and the direct path; docs/GUEST.md "Questions park
the task" is the guest side; `docs/spikes/temporal.md` has the M0 findings (versions, heartbeat caveat,
sandbox and test-server quirks). This page is how the pieces fit and how to
operate them on the box. Server walkthrough of the M5 exit criteria:
`RUNBOOK-M5.md`.

## 1. Who owns what

Temporal owns **orchestration**; the harness event log owns **what the model
saw**. They never hold the same fact twice.

| Temporal (workflow history in `temporal.db`) | Harness (session dir under `agent-api/data/sessions/`) |
|---|---|
| that a `task` / `answer` / `abort` signal arrived, and when | the `input` event the guest recorded for it (`input_id`, `dedupe`) |
| which activity ran, its attempts, timeouts, retries | the turns, tool calls, results, questions inside that activity |
| the last heartbeat detail of `run_task` (`{"last_seq": N}`) | the events up to and past that `seq` (the truth; the heartbeat is a floor) |
| the durable timer of a parked question (`answer_wait_s`) | the `question`, `checkpoint`, `parked` events and the VM snapshot |
| when a schedule fires (Temporal Schedule + `ScheduleTickWorkflow`) | the task the fire produced, like any other task |
| the outcome handed to `deliver` | the input's ticket `{session}/tickets/{ticket_id}.json` (written by `Worker.finish_input`, on either path), the `settle` event |

Retention: closed workflows are kept **30 days** on namespace `secondbrain`
(§6.1); the session dir is kept until it is deleted. A workflow that is gone
from Temporal loses nothing the model saw.

The path is configuration, never liveness: `TEMPORAL_ADDRESS` set → every
input of every session goes through the session workflow; unset → the direct
path (`Worker.act`) only, so tests and a Temporal-less box keep working. A
session never switches path. agent-api
depends on `flows` (editable path dependency `../flows`) and starts the
Temporal worker on its own loop in the background at startup
(`app/host/temporal.py`: `flows.worker.build_worker(client,
activities=[...real bodies...])`, then `flows.schedules.ensure_schedules`
with the loaded tenants doc); startup never waits for Temporal. A failed
connect, or a worker that exits on its own later, logs an error; a
supervisor tries again every `TEMPORAL_RECONNECT_S` (5). While it is not
ready `POST /chat` waits `TEMPORAL_READY_WAIT_S` (5), then answers 503 with
`Retry-After`. The worker runs at most `2 * HARNESS_MAX_CONCURRENT_TASKS +
10` activities; the task cap itself is the Workers' admission, shared with
the direct path (docs/HOST.md "Task cap"). One replica, task queue `secondbrain-agent`
(`flows.TASK_QUEUE`). Both paths run the same Worker task API and handle a
question park the same way, so every Worker gets the tenant's
`answer_wait_s`.

## 2. The session workflow

One `SessionWorkflow` per session, workflow id `{user}/{tenant}/{session_id}`
(`flows.session_workflow.workflow_id`), started by the first task with
signal-with-start (`flows.client.signal_task`) and the start argument
`SessionState(session, config=SessionConfig.from_tenant(tenant_entry))`. It
never completes on its own: it waits for the next task, and continues-as-new to
keep its history short.

```text
run(state):
  restore carried seen ids, queued tasks, and a parked task (+ its deadline)
  loop:
    continue-as-new if tasks_run >= max_tasks_per_run (50)
                     or (tasks_run > 0 and history >= max_history_events (10 000))
    wait for a queued task                         (task signal; duplicates dropped by input_id)
    prepare_vm(resume_from_park=False)             retry 3x, 60 s
    run_task(resume=False)                         heartbeat timeout 120 s, start_to_close = wall_clock_s + 30
                                                   + max_queue_wait_s (3600), schedule_to_close = the same, 2 attempts
    while result == parked(question_id):
      status -> parked(question_id); deadline = now + answer_wait_s (once per question)
      park_vm                                      retry 3x, 60 s (idempotent)
      wait for: answer(question_id) | abort | deadline | history full (after a signal)
        answer   -> prepare_vm(resume_from_park=True), run_task(resume=True, answer)
        abort    -> outcome aborted
        deadline -> outcome escalated
        history  -> continue-as-new carrying (task, parked result, deadline)
    deliver(outcome, question_id)                  retry 3x, 60 s; always runs
```

Outcomes (`flows.activities.Outcome`): `done`, `failed`, `timeout`,
`escalated` from the guest's `settle`; `parked` (never delivered: the loop
waits); `timeout` also when `run_task` hits its wall clock; `failed` when
`run_task`, `prepare_vm` or `park_vm` run out of attempts; `aborted` from an
`abort` signal (the guest settles it `aborted`, `answer: ""`, `detail` = the
reason); `escalated` also from the workflow's own timer (the guest settles it
`escalated` on `abort{reason: "escalated"}`). Every task
ends in exactly one `deliver`.

Details that matter:

- **Dedupe.** The workflow keeps the last 2000 `input_id`s (carried across
  continue-as-new). A redelivered POST or a retried schedule tick is dropped
  with a log line `duplicate task ignored`.
- **One task at a time.** Tasks arriving while one runs or is parked queue up
  (`status.pending`) and run in order after it.
- **Answers.** An `answer` whose `question_id` is not the parked one (stale,
  wrong, or the task is running) is ignored and logged as
  `answer for question 'x' ignored (parked on 'y')`. The status shows `parked`
  before `park_vm` has run, so an answer that arrives while the VM is being
  parked is kept.
- **Abort.** While running: cancels `run_task` (the host sends the guest
  `abort{reason: "aborted"}` and waits for its settle), delivers `aborted`.
  While parked: delivers `aborted` without resuming (deliver wakes the guest
  so it writes its own settle). While idle: ignored and logged.
- **Continue-as-new while parked** carries the task, its question and the
  **deadline** (`SessionState.in_flight: InFlight(task, question_id, deadline)`): a hop does not re-arm the
  full `answer_wait_s`. The new run calls `park_vm` again (idempotent) and
  waits for what is left; an expired deadline escalates at once.
- **Heartbeats.** The workflow never reads them. When `run_task` attempt 1
  dies (worker killed: heartbeat timeout after 120 s), the server starts
  attempt 2 with the last recorded details; the host re-attaches by
  `input_id` from that `last_seq` floor and never starts a second turn. The
  SDK throttles heartbeats, so after a SIGKILL the floor may be one interval
  old (spike).

### Signals, query

| Name | Payload | Effect |
|---|---|---|
| signal `task` | `TaskSignal{input_id, text, kind: "task"\|"schedule", wall_clock_s?}` | queue the task (dedupe by `input_id`) |
| signal `answer` | `AnswerSignal{question_id, text}` | resume the parked task if `question_id` matches |
| signal `abort` | `AbortSignal{reason}` | abort the running or parked task |
| query `status` | → `Status{state: idle\|running\|parked, current_input_id, question_id, seen, pending, tasks_run, generation}` | `GET /sessions/{id}` shows it as `workflow` |

Client helpers (`flows/client.py`): `signal_task`, `signal_answer`,
`signal_abort`, `query_status`, `session_handle`.

### Per-tenant config (`tenants.json`)

`SessionConfig.from_tenant(entry)`; a missing or `null` key keeps the default.

| Key | Default | Meaning |
|---|---|---|
| `answer_wait_s` | 86400 | how long a parked question waits before `escalated` (phone 24 h, browser-origin tenants 60; 0 = the guest never parks, it denies unanswered questions) |
| `wall_clock_s` | 600 (agent-api: `HARNESS_WALL_CLOCK_S`) | the task's wall clock: `RunTask.wall_clock_s`, in the guest's `task` message (the guest settles `timeout` at it); it counts from the task's start on the host, never its wait for a slot (the host's backstop is it + `WALL_CLOCK_GRACE_S` (30) from there). `TaskSignal.wall_clock_s` overrides per task; agent-api's chat route sets it from the tenant's current config |
| `max_tasks_per_run` | 50 | continue-as-new after this many tasks |
| `max_history_events` | 10 000 | continue-as-new once the history is this long |

Not a tenant key: `SessionConfig.max_queue_wait_s` (3600; agent-api sets it
from `HARNESS_MAX_QUEUE_WAIT_S`), the longest a task may wait to start
(Temporal's task queue, a slot of agent-api's task cap).

**`run_task` timeouts are a safety net, not the task's time limit.** The
limit is the wall clock, counted from when the task starts on the host
(after its slot): the guest settles `timeout` at it, the host's backstop is
it + 30 s. A task over the cap waits for a slot inside its `run_task`
activity, so the activity's start-to-close and schedule-to-close (all
attempts together) are `run_task_timeout_s` = `wall_clock_s +
WALL_CLOCK_GRACE_S + max_queue_wait_s`; they cannot fire during a normal
slot wait. Liveness is the heartbeat timeout (120 s): the host heartbeats
every `HARNESS_HEARTBEAT_S` from `start_task` on, also while the task is
queued. A `run_task` cancelled (abort) or timed out before its task started
drops the queued task on the host (`Worker.drop_queued`; `deliver` does the
same first if the cancel has not reached the activity yet): it never runs
later and no abort goes to a guest.
| `schedules` | none | §4 |

The config is fixed when the workflow **starts**: an edit applies to new
sessions (or to a session after `terminate`, §6.3), not to a running one.

## 3. Activities

Names and payloads are `flows/activities.py` (stubs there; change them only
additively). The real bodies are agent-api's `app/host/activities.py`; they
drive the Worker's task API as `act()` does on the direct path.

| Activity | Payload → result | Timeout / retries | Heartbeat |
|---|---|---|---|
| `prepare_vm` | `PrepareVM{session, input_id, resume_from_park}` | 60 s, 3 attempts | no |
| `run_task` | `RunTask{session, input_id, text, kind, resume, answer, question_id, wall_clock_s}` → `RunResult{outcome, question_id, last_seq}` | `wall_clock_s + WALL_CLOCK_GRACE_S + max_queue_wait_s` (start-to-close and schedule-to-close; a safety net, §2), 2 attempts | every 60 s, timeout 120 s, details `{"last_seq": N}` |
| `park_vm` | `ParkVM{session, input_id, question_id}` | 60 s, 3 attempts | no |
| `deliver` | `Deliver{session, input_id, outcome, question_id}` | 60 s, 3 attempts | no |
| `signal_session` | `SignalSession{session, task, task_queue, config}` → `input_id` | 30 s, 5 attempts | no |

What the host bodies do:

- `prepare_vm`: the session's Worker (tenant looked up by name); boot or wake
  the guest (`ensure_ready`: base restore or rootfs for a fresh session, the
  session snapshot for a parked one). Idempotent.
- `run_task`: heartbeat `{"last_seq": <acked guest seq>}` at start and every
  `HARNESS_HEARTBEAT_S` (60 s). On attempt > 1 the last heartbeat detail is a
  floor only (`floor_seq` on the `activity_start` host event). Resume: send
  `ui_response{question_id, answer}` (`Worker.answer`: input id
  `{input_id}:answer:{question_id}` on both paths, so a retry dedupes); no guest to resume
  a parked, unsettled task → a non-retryable failure. Then `start_task`: a
  running task for the input is re-attached, a settled one returns its stored
  settle, never a second turn. Returns the settle's outcome, or `parked` +
  `question_id` at the guest's question park. Cancelled (workflow abort,
  timeout) while its task still waits for a slot → the task is dropped
  (`Worker.drop_queued`, never runs, nothing to the guest); once started →
  `abort{reason: "aborted"}` to the guest, wait for its settle; re-raise.
- `park_vm`: make sure the session is parked and snapshotted (normally it
  already is: the worker adopted the guest's own park). Idempotent.
- `deliver`: an unsettled input whose task still waits for a slot: the task
  is dropped (the workflow's cancel of `run_task` reaches the activity only on
  its next heartbeat). For `escalated` / `aborted` with no guest settle (the answer
  timer, an abort while parked) first wake the guest and send
  `abort{reason: <outcome>}` so it writes the `settle` itself. Then
  `Worker.finish_input` (the one end of an input on both paths): the ticket
  `{session}/tickets/{ticket_id}.json`, written once = `{ticket_id,
  input_id, driver, outcome, answer, question_id, error, manifest, usage,
  completed_at}` (ticket_id = `derive_key_id("ticket", user, tenant,
  session_id, input_id)`: per input, not per settle seq, since seqs restart
  with every guest run), then the input's SSE streams end (`error` unless
  `done`, then `done`); the host event `workflow{kind: delivered, input_id,
  outcome, ticket_id}` once. Idempotent by `input_id`.

`signal_session` is the schedule glue (`flows.schedules.SessionSignaller`), the
only activity that talks to Temporal; `build_worker` registers it next to the
four host activities.

## 4. Schedules

```json
"schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily", "tz": "Europe/London"}]
```

`cron` and `prompt` are required; `session` defaults to `daily`, `tz` to
`UTC`. agent-api calls `flows.schedules.ensure_schedules(client, tenants_doc)`
at startup: one Temporal Schedule per entry, id `{tenant}/{session}/{index}`
(index = position in the tenant's list), created, or updated in place when
it changed. "Changed" is one fingerprint: the action's only memo key,
`fingerprint`, holds a sha256 over everything the schedule is built from
(id, workflow type, task queue, user/tenant/session, cron, tz, prompt, the
tenant's `SessionConfig`, the policy); a schedule whose stored fingerprint
equals the new one is not rewritten, and one without a fingerprint is
rewritten once;
schedules of a listed tenant that are no longer in the file are deleted. A
malformed entry or a tenant with a bad config is logged and skipped. Returns
`{schedule_id: created|updated|unchanged|deleted}`.

A fire starts `ScheduleTickWorkflow` (id `{schedule_id}-{fire time}`, e.g.
`default/daily/0-2026-10-10T06:00:00Z`) whose `signal_session` activity
signal-with-starts `{user}/{tenant}/{session}` with a `task` of kind
`schedule`, text = the prompt, `input_id` = the tick's workflow id (so a retry
cannot double-run it). A Schedule action cannot signal-with-start directly
(spike). Missed fires are skipped (catch-up window 10 s, overlap SKIP): a
server that was down at 07:00 does not run the 07:00 brief at 09:00. The fire
time is evaluated in `tz` (07:00 Europe/London is 06:00Z in summer).

## 5. Server

Compose service `temporal` (`docker-compose.yml`): `temporalio/temporal:1.9.1`
(CLI 1.9.1, Server 1.32.0, UI 2.54.1) running
`server start-dev --ip 0.0.0.0 --db-filename /data/temporal.db --namespace secondbrain --ui-port 8233`.

- State: `./agent-api/data/temporal/temporal.db` (SQLite). The image runs as
  uid 1000; create the directory owned by it before the first start:
  `mkdir -p agent-api/data/temporal && sudo chown 1000:1000 agent-api/data/temporal`.
- gRPC 7233 on `llm_internal` only (agent-api: `TEMPORAL_ADDRESS=temporal:7233`,
  `TEMPORAL_NAMESPACE=secondbrain`); never published, not on `tunnel_network`.
- UI on `127.0.0.1:8233` of the box only. From a laptop:
  `ssh -L 8233:localhost:8233 <box>` then http://localhost:8233/namespaces/secondbrain/workflows.
- agent-api `depends_on: temporal (service_started)`: it does not wait for
  healthy; while Temporal is down `POST /chat` answers 503 + `Retry-After`
  (never the direct path, §1).
- Healthcheck: `temporal operator cluster health` (`docker compose ps` shows it).
- Back up: stop the service, copy `temporal.db` (SQLite, single file).
- Moving off the home server: `temporalio/auto-setup` + Postgres + `temporalio/ui`,
  same worker code (PLAN 10).

## 6. CLI runbook

The CLI is in the container; every command below runs on the box from the
repo root (all were run against `temporalio/temporal:1.9.1`). The CLI's
default namespace is `default`, so every command names `--namespace secondbrain`.

```sh
W=owner/default/2026-10-09T07-00-00      # {user}/{tenant}/{session_id}
```

### 6.1 Once after the first start: retention 30 days

```sh
docker compose exec temporal temporal operator namespace update --namespace secondbrain --retention 720h
docker compose exec temporal temporal operator namespace describe --namespace secondbrain | grep -i retention
#   Config.WorkflowExecutionRetentionTtl  720h0m0s
```

The dev server creates `secondbrain` with 24 h retention only when it does
not exist yet; the setting lives in `temporal.db` and survives restarts
(checked). Re-run it after deleting `temporal.db`.

### 6.2 Look

```sh
# open sessions / everything
docker compose exec temporal temporal workflow list --namespace secondbrain \
    --query 'ExecutionStatus="Running" AND WorkflowType="SessionWorkflow"'
docker compose exec temporal temporal workflow list --namespace secondbrain --limit 20

# one session: status query (idle | running | parked, question, pending, generation)
docker compose exec temporal temporal workflow query --namespace secondbrain -w "$W" --type status

# describe: run id, history length, Pending Activities (run_task: Attempt,
# HeartbeatDetails {"last_seq": N}, LastHeartbeatTime), pending timer when parked
docker compose exec temporal temporal workflow describe --namespace secondbrain -w "$W"

# history: signals, activity attempts, TimerStarted (the parked wait), ContinuedAsNew
docker compose exec temporal temporal workflow show --namespace secondbrain -w "$W"
docker compose exec temporal temporal workflow show --namespace secondbrain -w "$W" --detailed   # payloads
docker compose exec temporal temporal workflow show --namespace secondbrain -w "$W" --output json > /tmp/history.json

# schedule ticks of one schedule
docker compose exec temporal temporal workflow list --namespace secondbrain \
    --query 'WorkflowType="ScheduleTickWorkflow"'
```

`describe`/`show`/`query` without `--run-id` follow the latest run, so they
keep working across continue-as-new.

### 6.3 Act

```sh
# answer a parked question (what POST /chat {answer_to} does)
docker compose exec temporal temporal workflow signal --namespace secondbrain -w "$W" \
    --name answer --input '{"question_id": "<from status>", "text": "blue"}'

# abort the running or parked task (what POST /sessions/{id}/abort does)
docker compose exec temporal temporal workflow signal --namespace secondbrain -w "$W" \
    --name abort --input '{"reason": "ops"}'

# inject a task by hand (input_id must be new, or it is dropped as a duplicate)
docker compose exec temporal temporal workflow signal --namespace secondbrain -w "$W" \
    --name task --input '{"input_id": "manual-1", "text": "hello", "kind": "task"}'

# end a session workflow (no deliver runs; the next POST /chat starts a fresh
# workflow with the tenant's current config; the session dir is untouched)
docker compose exec temporal temporal workflow terminate --namespace secondbrain -w "$W" --reason "ops"

# reset: replay the workflow from an earlier point (e.g. after a workflow-code
# bug left it stuck); signals after the point are re-applied
docker compose exec temporal temporal workflow reset --namespace secondbrain -w "$W" \
    --type LastWorkflowTask --reason "ops"
docker compose exec temporal temporal workflow reset --namespace secondbrain -w "$W" \
    --event-id <N from show> --reason "ops"
```

`reset` re-runs activities scheduled after the reset point. `deliver` and
`park_vm` are idempotent and `run_task` re-attaches by `input_id`, so this is
safe, but prefer `abort` or `terminate` for a stuck task; reset is for a stuck
workflow.

### 6.4 Schedules

```sh
docker compose exec temporal temporal schedule list --namespace secondbrain
docker compose exec temporal temporal schedule describe --namespace secondbrain -s default/daily/0
#   Spec [{"minute":"0","hour":"7",...}]  TimeZoneName Europe/London  NextRunTime ...  ActionCounts ...
docker compose exec temporal temporal schedule trigger --namespace secondbrain -s default/daily/0   # fire now
docker compose exec temporal temporal schedule toggle --namespace secondbrain -s default/daily/0 --pause --reason "holiday"
docker compose exec temporal temporal schedule toggle --namespace secondbrain -s default/daily/0 --unpause --reason "back"
docker compose exec temporal temporal schedule delete --namespace secondbrain -s default/daily/0
```

`tenants.json` is the source of truth: agent-api's startup re-creates a
deleted schedule and overwrites a hand edit (a pause is overwritten too).
To stop a schedule for good, remove it from `tenants.json` and restart
agent-api.

### 6.5 Worker without agent-api (smoke test)

7233 is not published, so run the stub worker where `temporal:7233`
resolves, i.e. in the agent-api container (it has `flows` installed), on a
task queue of its own so it never picks up real work:

```sh
docker compose exec -e TEMPORAL_TASK_QUEUE=smoke agent-api python -m flows.worker
#   flows worker on smoke (secondbrain); Ctrl-C to stop
```

Stub activities record calls in memory and return `done`. Never run it on
`secondbrain-agent` next to agent-api: both would poll the real queue.

## 7. Tests

```sh
cd flows && uv sync && uv run pytest -q          # time-skipping test server (Java, auto-downloaded)
FLOWS_TEST_ENV=local uv run pytest -q            # CLI dev server (auto-downloaded)
FLOWS_TEST_ENV=external TEMPORAL_TEST_ADDRESS=localhost:7233 uv run pytest -q   # a running server
```

`flows/tests/test_session_workflow.py` covers dedupe, park → answer →
resume, wrong-question answers, carried parked tasks, timer escalation,
heartbeat details on retry, continue-as-new by task count and by history
length (also while parked, with the deadline carried), abort while running
and while parked, the status query and schedule-kind tasks;
`test_schedules.py` covers `entries_from_tenants` (defaults, skips, the
shipped `tenants.json.example`) and `ensure_schedules` against a real dev
server (create, update, delete stale, trigger → session task).
