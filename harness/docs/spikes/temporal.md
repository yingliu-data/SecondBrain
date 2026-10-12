# Spike: Temporal as the control plane (M0 → M5)

Findings for PLAN D11, D12, D22 and section 10. Code: `flows/` (uv project,
package `flows`). Date: 2026-10-07.

## What was run

| What | Version |
|---|---|
| Python | 3.12.3, uv 0.12.3 |
| `temporalio` (Python SDK, pulls `temporalio_sdk_core` in the wheel) | 1.34.0 (+ `nexus-rpc` 1.4.0, `protobuf` 7.36.2) |
| Time-skipping test server (Java, auto-downloaded by `WorkflowEnvironment.start_time_skipping()` to `/tmp/temporal-test-server-sdk-python-1.34.0`, 84 MB) | bundled with SDK 1.34.0 |
| `temporal` CLI dev server (auto-downloaded by `start_local()` to `/tmp/temporal-sdk-python-1.34.0`, 156 MB) | CLI 1.9.1, Server 1.32.0, UI 2.54.1 |
| Docker image `temporalio/temporal:latest` (same dev server) | CLI 1.9.1, Server 1.32.0, UI 2.54.1 |

```
cd flows && uv run pytest -q                  # 12 passed in 11 s (time-skipping server; `uv sync --all-packages` at the root first)
FLOWS_TEST_ENV=local uv run pytest -q          # 12 passed in 13 s (CLI dev server, in-memory)
FLOWS_TEST_ENV=external TEMPORAL_TEST_ADDRESS=localhost:17233 uv run pytest -q
                                               # 12 passed in 21 s (docker dev server, SQLite file)
```

The test fixture (`flows/tests/conftest.py`) tries the three in that order and
prints `flows env: <which>`; all three worked here because the box has
network. Both binaries download once and are cached in `/tmp`.

Tests (`flows/tests/test_session_workflow.py`, `test_schedules.py`), all green
on all three servers:

| | Test | Checks |
|---|---|---|
| a | `test_a_duplicate_task_signal_runs_once` | two `task` signals with the same `input_id` → one `run_task`, one `deliver`, `status.seen == 1` |
| b | `test_b_parked_then_answer_resumes` | `run_task` returns `parked` → `park_vm` → status `parked` with `question_id` → `answer` signal → `prepare_vm(resume_from_park=True)` → `run_task(resume=True, answer="42")` → `deliver(done)` |
| b2 | `test_b2_answer_for_wrong_question_is_ignored` | an `answer` for another `question_id` leaves the workflow parked |
| c | `test_c_parked_timer_expiry_escalates` | parked with `answer_wait_s = 24 h`, `env.sleep(24 h + 1 s)` skips time in 0.1 s real time → `deliver(escalated)`, no second `run_task` (2 s real wait on the non-skipping servers) |
| d | `test_d_run_task_retry_receives_heartbeat_details` | `run_task` raises on attempt 1 after heartbeating seq 1..3; attempt 2 sees `activity.info().heartbeat_details == [3]`; `prepare_vm` ran once (the retry is the activity's, the workflow is not re-entered) |
| e | `test_e_continue_as_new_after_n_tasks` | `max_tasks_per_run = 2`, three tasks → new run id, `generation == 1`, `seen == 3` carried, a resend of `in-1` is still deduped after the hop |
| f | `test_f_status_query` | `status` → `running` with `current_input_id` during a task, `idle` after; `seen`, `pending`, `tasks_run`, `generation` |
| g | `test_g_abort_while_running` | `abort` signal cancels the running `run_task`, `deliver(aborted)`, back to `idle` |
| h | `test_h_schedule_kind_task` | a `task` of kind `schedule` runs like any other |
| s | `test_ensure_schedules_create_update_trigger_delete` | `ensure_schedules` creates `t/daily/0` and `t/daily/1`; second call with one entry → `updated` + `deleted`; a manual `trigger()` runs `ScheduleTickWorkflow` → `signal_session` → `SessionWorkflow` receives `kind="schedule"` with `input_id = "t/daily/0-<fire time>"`; tz `Europe/London` honoured (07:00 London = 06:00 UTC next-fire) |

Plus a manual spike against the docker dev server (`scratchpad/spike_restart.py`,
not kept): a worker process SIGKILLed mid-`run_task`, a second worker picks up
the retry; and a parked workflow with a 24 h timer inspected with
`temporal workflow describe` and the UI.

## Does the heartbeat detail survive a retry? Yes, with one caveat

The mechanism, verified on all three servers:

- The activity calls `activity.heartbeat(seq)`; the server stores the latest
  details on the pending activity (visible in `describe` → `pending_activities[].heartbeat_details`).
- When attempt 1 fails (raised error, heartbeat timeout after a worker death,
  start_to_close), the server schedules attempt 2 *inside the same activity*:
  no history events are written for the failed attempt, and the new attempt's
  `activity.info().heartbeat_details` holds the stored details.
  `info().attempt == 2`.
- The workflow never sees the details. It `await`s one activity handle and gets
  the final `RunResult`; the `retry_policy(maximum_attempts=2)` is the whole
  hand-off. (The stub records the heartbeat details it received in `CALLS`,
  which is what the test asserts on.) The workflow is not re-entered and `prepare_vm` is not
  re-run — exactly PLAN 4.5: the retry attaches to the still-running guest.

The caveat: **the SDK throttles heartbeats**. Core sends the first heartbeat
immediately, then at most one per throttle interval = `0.8 × heartbeat_timeout`
capped by `Worker(max_heartbeat_throttle_interval=60 s)`; intermediate values
are buffered and the latest one is flushed when the activity completes or
*fails by raising*. So:

- Raised failure (test d): the retry saw the **last** seq (3 of 3).
- SIGKILL of the worker (spike): the retry saw seq **1** although the stub had
  heartbeated ~15 times in 1.5 s — buffered heartbeats died with the process.
  With the production cadence (heartbeat every 60 s, timeout 120 s → throttle
  60 s) each heartbeat is sent, but the resume point can still be up to one
  interval old. The guest-side resume must therefore accept any older acked
  `seq` and re-send everything after it (section 4.5 already says this; this
  is the reason it must stay true).
- The Java test server logs a warning that it does not advertise
  `activity_failure_include_heartbeat` (server ≥ 1.16 does); in practice the
  details were present there too. The real server (1.32) does not warn.

Timing of a worker death: with the run_task heartbeat timeout set to 2 s, the retry started
~13 s after the kill. Two things add up: the server's heartbeat-timeout check
is coarse (seconds), and the first workflow task after the retry was first
dispatched to the dead worker's **sticky queue** and had to time out there
(history shows `WORKFLOW_TASK_SCHEDULED, WORKFLOW_TASK_TIMED_OUT` before the
normal-queue one). With the real 120 s heartbeat timeout this is noise; with
one agent-api replica the sticky timeout (default 10 s) is the floor for
"worker restarted, work resumes".

## What a 24 h timer costs: nothing

A parked task is `park_vm` → `workflow.wait_condition(..., timeout=answer_wait_s)`.
On the server that is one `TIMER_STARTED` event (`start_to_fire_timeout: 1 day`)
in the history, `Pending Activities: 0`, status `Running`. The worker holds no
task for it (only its normal long-poll on the queue), and the workflow stays
open with the worker process stopped (verified: `stop_worker`, describe still
`RUNNING`). Resume on `answer` is one signal → one workflow task. Time-skipping
makes the 24 h test take 0.1 s.

Costs to be aware of instead: every signal, activity attempt and timer is
~3–6 history events; a session's history grows by roughly 15 events per task
(more with park/resume), which is why continue-as-new at 50 tasks keeps a run
well under the 10 000-event warning (PLAN says 50 tasks or 10 000 events —
`workflow.info().get_current_history_length()` can add the second trigger in M5).

## Proposed compose snippet

The dev server image bundles the UI (`:8233`), so one service is enough on the
box; a separate `temporal-ui` service is for the `auto-setup` + Postgres move.
Not applied to `docker-compose.yml` (out of this spike's ownership).

```yaml
  # -- Temporal (control plane: session workflows, timers, schedules) ------
  temporal:
    image: temporalio/temporal:1.9.1          # CLI 1.9.1 = Server 1.32.0, UI 2.54.1
    container_name: secondbrain-temporal
    restart: unless-stopped
    command:
      - server
      - start-dev
      - --ip=0.0.0.0
      - --db-filename=/data/temporal.db      # SQLite on the bind-mounted volume
      - --namespace=secondbrain              # created at start (default ns too)
      - --ui-port=8233
      # retention: dev server default is 24 h for `default`; set 30 d for ours
      # once up:  temporal operator namespace update --retention 720h secondbrain
    volumes:
      - ./agent-api/data/temporal:/data      # file is created as uid 1000; chown or pre-create the dir
    ports:
      - "127.0.0.1:8233:8233"                # UI, localhost only
    networks:
      - agent-net                            # agent-api reaches it as temporal:7233
    healthcheck:
      test: ["CMD", "temporal", "operator", "cluster", "health", "--address", "localhost:7233"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s

  # Later, off the home server (not now): temporalio/auto-setup + postgres and
  #   temporal-ui:
  #     image: temporalio/ui:2.54.1
  #     environment: [TEMPORAL_ADDRESS=temporal:7233, TEMPORAL_CORS_ORIGINS=http://localhost:3000]
  #     ports: ["127.0.0.1:8233:8080"]
```

agent-api additions: `TEMPORAL_ADDRESS=temporal:7233`, `TEMPORAL_NAMESPACE=secondbrain`,
`depends_on: temporal: condition: service_healthy`. `temporal` must not be on
`tunnel_network`.

Verified here with `docker run --rm -d -p 127.0.0.1:17233:7233 -p 127.0.0.1:18233:8233
-v $DIR:/data temporalio/temporal:latest server start-dev --ip 0.0.0.0
--db-filename /data/temporal.db --namespace secondbrain`: `temporal.db` appears
on the volume (owned by uid 1000), the UI answers 200, `temporal workflow
list/describe --namespace secondbrain` work from inside the container, and the
full test suite passes against it.

## SDK sandbox restrictions hit

The workflow sandbox (`SandboxedWorkflowRunner`, on by default) re-imports a
workflow's module in a restricted importer per run and refuses non-deterministic
calls. What came up:

1. `flows.activities` (dataclasses only) is imported inside
   `with workflow.unsafe.imports_passed_through():` in `session_workflow.py`.
   Without it the sandbox re-imports the module per workflow run (slow, and
   `activities.CALLS` would be a different object inside the sandbox — harmless
   because the workflow never touches it, but confusing). `temporalio.*` and
   the stdlib are passed through by default.
2. Everything that needs a `Client` (signal-with-start, schedules, the
   `signal_session` activity) lives outside the workflow module: `flows/client.py`,
   `flows/schedules.py`. Importing `temporalio.client` from a workflow file would
   pass the sandbox but is the wrong layer; keeping `session_workflow.py` to SDK
   + stdlib + `flows.activities` is what PLAN section 10 asks.
3. Activities are referenced by **name** (`"run_task"`) in
   `workflow.start_activity(...)` with `result_type=RunResult`, not by function
   object, so agent-api can register its own bodies under the same names without
   the workflow module importing `app/`.
4. `random`, `datetime.now`, `time.time` were not needed; `workflow.now()`
   would be the sandbox-safe form if M5 needs timestamps in the workflow.
5. A **Schedule action cannot signal-with-start** and has no id-conflict
   policy in SDK 1.34 (`ScheduleActionStartWorkflow` has `workflow, args, id,
   task_queue, timeouts, retry_policy, memo, search attributes, priority`). A
   schedule that started `SessionWorkflow` directly would fail whenever the
   session's workflow is already running. Hence `ScheduleTickWorkflow` + the
   `signal_session` glue activity (`SessionSignaller(client)`), the one activity
   that talks to Temporal.
6. **Time-skipping test server quirks**: (a) `env.sleep()` only skips time when
   every open workflow is blocked on a timer; an activity left pending with no
   worker to run it (or a `terminate` racing an in-flight activity) makes every
   later `env.sleep` fail with `RPCError: Timeout expired` after 30 s. The test
   fixture waits for `status != running` before terminating. (b) The server
   stores cron expressions as structured calendars (`describe` returns
   `calendars`, `cron_expressions == []`). (c) No schedule support on the Java
   test server; the schedules test uses `start_local()`.
7. pytest-asyncio 1.4: a session-scoped async fixture holding the client needs
   `asyncio_default_test_loop_scope = "session"` as well, otherwise tests run on
   another loop and every gRPC call hangs.

## Open questions for M5

1. **`answer_wait_s`** per tenant: 24 h (phone) and 60 s (guest) as the plan says,
   or days for the phone? Cost is nil either way; the question is product.
   Also: after `escalated`, does the task's question get re-asked by a later
   task, or is the ticket closed?
2. **Multiple questions per task**: the workflow loops `park → wait → resume`
   while `run_task` keeps returning `parked`; D12's "three unanswered →
   escalated" needs a counter per task (trivial) — confirm the count is per
   task, not per session.
3. **Abort semantics**: today `abort` cancels the running activity and
   `deliver(aborted)`. The real `run_task` must honour cancellation (stop the
   guest, snapshot or destroy?) — decide what the VM does on abort.
4. **Resume point staleness**: because of heartbeat throttling, the retry's
   seq can be one heartbeat interval old. Heartbeat on every acked `seq`
   batch is fine (Core throttles the wire), but the host-side resume must
   tolerate re-sent events (it should already: seq-based ack).
5. **Continue-as-new while parked**: carried in `SessionState.in_flight` (the
   task and its parked `RunResult`) and resumed through the normal run loop; the 24 h timer restarts from the new run's start, so a
   hop while parked re-arms the full wait. Acceptable, or carry the deadline?
   Also add the `history_length` trigger next to the 50-task one.
6. **Workflow id charset**: `{user}/{tenant}/{session_id}` with slashes works
   (Temporal allows any string); the UI URL-encodes it. Session ids are
   `2026-10-07T07-00-00`-style, fine.
7. **Retention**: the dev server namespace defaults to 24 h retention for
   closed workflows; `temporal operator namespace update --retention 720h
   secondbrain` after first start, or create the namespace from agent-api at
   boot (`client.service_client.operator_service` / `register_namespace`).
8. **Worker in the agent-api process** (D22): `start_worker(client, activities=[...])`
   returns `(worker, task)` running on the current loop; agent-api should call
   `stop_worker(worker, task)` in its shutdown hook. One replica → one worker; a stale
   sticky queue costs ~10 s after a crash (above).
9. **Schedules and users**: a tenant's schedule signals
   `{tenant.user}/{tenant}/{session}`; `entries_from_tenants` falls back to
   `user = name` like agent-api's `Tenant` model. Confirm that is the intended
   session for the morning brief (`daily`) and whether schedules may name a
   different user.
10. **`submit_task` update** (optional in the plan): not built; `signal_task`
    returns as soon as the server has the signal, which is the acknowledgement
    a non-SSE client needs. An Update would only add "the workflow has
    *accepted* it", i.e. dedupe result; cheap to add if the iOS app wants it.

## CLI runbook seeds (for `docs/FLOWS.md`)

```
temporal workflow list      --namespace secondbrain
temporal workflow describe  --namespace secondbrain -w alice/default/daily
temporal workflow signal    --namespace secondbrain -w alice/default/daily --name task \
    --input '{"input_id":"manual-1","text":"hello","kind":"task"}'
temporal workflow query     --namespace secondbrain -w alice/default/daily --type status
temporal workflow terminate --namespace secondbrain -w alice/default/daily --reason "ops"
temporal workflow reset     --namespace secondbrain -w alice/default/daily --type LastWorkflowTask
temporal schedule list      --namespace secondbrain
temporal schedule trigger   --namespace secondbrain -s default/daily/0
```
