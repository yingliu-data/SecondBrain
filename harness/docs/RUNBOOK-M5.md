# M5 runbook: Temporal, questions, schedules on the server

Follows `RUNBOOK-M3.md` (base snapshot, park and resume work on the box) and
the M4 gateway setup (`GATEWAY.md`). Verifies the M5 exit criteria of PLAN.md
section 13. Host API and the direct path: `docs/HOST.md`; how the workflow works and the
full CLI: `docs/FLOWS.md`. The `flows/` workflow logic is tested on the
time-skipping test server (`cd flows && uv run pytest -q`); the steps below
are the live run with the real activities, guest and VM.

Exit criteria:

1. A task that asks a question waits 30 minutes with the VM parked, and
   finishes after the answer.
2. A 10-minute fake MCP tool completes with no client connected, and the
   Temporal UI shows heartbeats, then completion.
3. Kill agent-api mid-task: the activity retries from its heartbeat `seq` and
   the task finishes with no duplicate turn.
4. The morning-brief schedule fires.

Host log records are `{"ts", "hseq", "session_id", "type", "body"}` lines in
`{session}/events/host-000001.jsonl`; guest events are in
`{session}/events/rNNNN/000001.jsonl` (`r0001` for a new session).

## 0. Prerequisites (once)

```sh
cd ~/SecondBrain                                   # the compose project root
git pull && docker compose pull agent-api          # an image with flows + the M5 host
mkdir -p agent-api/data/temporal && sudo chown 1000:1000 agent-api/data/temporal
docker compose up -d temporal
docker compose ps temporal                          # (healthy) after ~20 s
docker compose exec temporal temporal operator namespace update --namespace secondbrain --retention 720h
docker compose exec temporal temporal operator namespace describe --namespace secondbrain | grep -i retention
#   Config.WorkflowExecutionRetentionTtl  720h0m0s
```

`agent-api/data/tenants.json`, default tenant (the one the phone uses), an
entry of the `tenants` list:

```json
{
  "name": "default", "api_key": "<API_SECRET_KEY>", "is_default": true,
  "answer_wait_s": 86400,
  "schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily", "tz": "Europe/London"}]
}
```

(`answer_wait_s` 86400 is also the default when the key is absent,
`flows.activities.SessionConfig`. An `engine` key is ignored with a warning
since M6.)

Then `docker compose up -d agent-api` (compose sets `TEMPORAL_ADDRESS=temporal:7233`,
`TEMPORAL_NAMESPACE=secondbrain`). Check the startup:

```sh
docker compose logs agent-api | grep -E '\[host.temporal\]'
#   ... [host.temporal] INFO: Temporal worker on secondbrain-agent (secondbrain)
#   ... [host.temporal] INFO: schedules: {'default/daily/0': 'created'}
#   ('unchanged' on later restarts; 'updated' after a schedule edit)
#   failure instead: [host.temporal] ERROR: Temporal at temporal:7233 (secondbrain) unreachable, direct path stays: ...
docker compose exec temporal temporal schedule list --namespace secondbrain
#   default/daily/0  {"Workflow":"ScheduleTickWorkflow"}  false  <next 07:00 London>
```

The UI from a laptop: `ssh -L 8233:localhost:8233 <box>`, then
http://localhost:8233/namespaces/secondbrain/workflows.

Shorthand for the steps below (`U` is the default tenant's `user`, the
directory under `agent-api/data/sessions/`: its `user` key, else its name).
agent-api publishes no host port and its image has no `curl`, and
`/api/v1/*` wants Bearer + `X-Timestamp` + `X-Signature` (HMAC-SHA256 of
timestamp + body, `app/auth/middleware.py`), so requests run inside the
container (`MAX_TIME=20 chat ...` drops the client after ~20 s):

```sh
U=$(jq -r '[.tenants[] | select(.is_default)][0] | .user // .name // "default"' agent-api/data/tenants.json)
api() {   # api PATH [JSON]: GET without a body, POST with one; the response streamed
  docker compose exec -T -e KEY="${KEY:-}" -e MAX_TIME="${MAX_TIME:-}" agent-api python - "$@" <<'EOF'
import hashlib, hmac, os, sys, time, urllib.request
key = os.environ.get("KEY") or os.environ["API_SECRET_KEY"]   # KEY=<tenant key> for another tenant
path, body = sys.argv[1], (sys.argv[2].encode() if len(sys.argv) > 2 else b"")
ts = str(int(time.time()))
sig = hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://localhost:8000" + path, data=body or None, headers={
    "Authorization": f"Bearer {key}", "X-Timestamp": ts, "X-Signature": sig,
    "Content-Type": "application/json"})
end = time.monotonic() + float(os.environ.get("MAX_TIME") or "inf")
with urllib.request.urlopen(req) as r:
    for line in r:                  # SSE keepalives arrive every 15 s
        sys.stdout.write(line.decode()); sys.stdout.flush()
        if time.monotonic() > end:
            break                   # drop the client; the task runs on
EOF
}
chat() { api /api/v1/chat "$1"; }
tq() { docker compose exec -T temporal temporal "$@" --namespace secondbrain; }
hostev() { jq -c "select(.type==\"$2\") | .body" "$1"/events/host-*.jsonl; }   # hostev $S workflow
```

## 1. Exit 1: a question parks for 30 minutes, then the task finishes

```sh
S=agent-api/data/sessions/$U/default_m5q
W=$U/default/m5q
# start the task and drop the stream once the question arrives (no client attached)
chat '{"session_id": "m5q", "input_id": "q-1",
       "message": "Ask me with the question tool which colour I like, then write it to colour.txt"}' \
  | tee /tmp/m5q.sse | grep -m1 'event: question'
# Ctrl-C if it does not exit by itself
tq workflow query -w "$W" --type status
#   "state":"running" until the guest's question deadline passes, then:
#   {"state":"parked","current_input_id":"q-1","question_id":"<qid>",...}
```

After the guest's question deadline the guest parks itself. The deadline is
`hello.config.question_timeout_s` = `HARNESS_QUESTION_TIMEOUT_S` (default
**300 s**), capped by an MCP input request's `expires_in_s`; with
`answer_wait_s > 0` it is no deny: the guest logs
`clock{kind: question_deadline, ref: <qid>}`, keeps the question open and
parks (`parked{reason: "question", question_id, wall_used_ms,
question_left_ms}`), the worker adopts the park and snapshots it, and
`run_task` returns `parked`. The task's wall clock and the question deadlines
are paused while parked. Check:

```sh
QID=$(tq workflow query -w "$W" --type status -o json | jq -r '.queryResult[0].question_id')
echo "$QID"
tail -3 $S/events/r0001/0*.jsonl | jq -c '{seq, type, reason: .body.reason, question_id: .body.question_id}'
#   ... checkpoint, parked{reason:"question", question_id: <qid>}
hostev $S workflow | grep -E '"parked"|activity_start'   # parked{reason: question, question_id, snapshot: true}
ls $S/vm/snapshot/                                    # the park snapshot
pgrep -af "firecracker.*default-m5q" || echo "no VM process"       # parked: nothing running
tq workflow show -w "$W" | tail -4                    # ... ActivityTaskCompleted (park_vm), TimerStarted
tq workflow describe -w "$W" | grep -A3 "Pending Activities"   # 0: a parked task holds no activity
```

Wait **30 minutes** (optionally `docker compose restart agent-api` in the
middle: the timer and the parked state live in Temporal and on disk). Then
answer:

```sh
chat "{\"session_id\": \"m5q\", \"answer_to\": \"$QID\", \"message\": \"blue\"}" | tee /tmp/m5q-2.sse
#   ... the continuation streams, ends with event: done
```

Pass:

- history: `WorkflowExecutionSignaled` (answer) ~30 min after `TimerStarted`,
  then `prepare_vm` (`resume_from_park: true`, see `tq workflow show -w "$W" --detailed`),
  `run_task` (`resume: true, answer: "blue"`), `deliver` (`outcome: done`);
  `tq workflow query -w "$W" --type status` → `idle`, `tasks_run: 1`;
- host events: `workflow{kind: parked, reason: question}`, then
  `workflow{kind: answer_signal, question_id: <qid>, sent: "held", vm_state:
  "parked"}` (held = kept for the wake, `ui_response` first),
  `vm{op: restore, source: session}` and
  `workflow{kind: delivered, outcome: done}`;
- guest log: `clock{kind: resumed}` at parked `seq` + 1, then the
  `input{kind: ui_response, question_id: <qid>, answer: "blue"}`, no second
  `session_start`, one `settle{outcome: done}`;
- ticket: `jq . $S/tickets/*.json` → `outcome: "done"`, `input_id: "q-1"`;
- `colour.txt` in the workspace says blue.

Negative checks (short): an `answer` signal with a wrong `question_id`
leaves it parked (`tq workflow signal -w "$W" --name answer --input '{"question_id":"nope","text":"x"}'`,
the agent-api log shows `answer for question 'nope' ignored (parked on
'<qid>')`); repeating the run with `"answer_wait_s": 120` on a test tenant
delivers `escalated` two minutes after the park (host event
`workflow{kind: answer_timeout}`), with a ticket and a guest
`settle{outcome: escalated}`.

Without Temporal (`TEMPORAL_ADDRESS` unset or unreachable: the direct path)
the same question parks the same way: `answer_wait_s` reaches the hello on
both paths, and `act()` waits up to `answer_wait_s` for the answer
(`POST /api/v1/chat` with `answer_to`) before `abort{escalated}`.

## 2. Exit 2: a 10-minute MCP tool, no client, heartbeats in the UI

A fake upstream with one slow tool, on the host:

```sh
mkdir -p ~/m5-slow && cat > ~/m5-slow/slow_mcp.py <<'EOF'
import asyncio
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("slow", host="0.0.0.0", port=8799)

@mcp.tool()
async def slow_job(minutes: int = 10) -> str:
    """Wait the given number of minutes, then report done."""
    await asyncio.sleep(minutes * 60)
    return f"slow_job finished after {minutes} min"

mcp.run(transport="streamable-http")
EOF
cd ~/m5-slow && uv run --with mcp python slow_mcp.py &      # serves http://0.0.0.0:8799/mcp
```

Register it (restart both after editing):

- `mcp-gateway/data/gateway.json` (the gateway owns MCP access; agent-api
  ignores `mcp_servers` since M6): upstream
  `"slow": {"url": "http://host.docker.internal:8799/mcp", "timeout_s": 900, "allow_private": true}`,
  `"slow"` in `tenants.default.upstreams`, and `"slow__slow_job"` in
  `tenants.default.promoted` so the model sees the tool by name (otherwise
  it reaches it through the `search_tools` / `call_tool` meta tools);
- `agent-api/data/tenants.json`, on the default tenant: `"wall_clock_s": 1200`
  (`run_task`'s start-to-close and the guest's `limits.wall_clock_s`: the
  600 s default would time the task out before the tool returns).

Every per-call ceiling on the way must exceed the 600 s the tool takes:

| Ceiling | Where | Value here |
|---|---|---|
| upstream call timeout | gateway.json `upstreams.slow.timeout_s` (gateway's upstream deadline) | 900 |
| guest op timeout of the call | the hello entry's `timeout_s`, which the gateway sets to the upstream's `timeout_s` (meta tools: the largest of the tenant's upstreams); `limits.op_timeout_s.mcp` (300) applies only to an entry without one | 900 |
| forwarder read timeout (:5011 -> gateway) | `MCP_GATEWAY_CALL_TIMEOUT_S` on agent-api | 3600 (default) |
| task wall clock | tenant `wall_clock_s` (Temporal path; the direct path uses `HARNESS_WALL_CLOCK_S`) | 1200 |
| `run_task` heartbeat timeout | flows (120 s), fed every `HARNESS_HEARTBEAT_S` (60) | not a call ceiling |

```sh
docker compose restart mcp-gateway agent-api
W=$U/default/m5slow
MAX_TIME=20 chat '{"session_id": "m5slow", "input_id": "s-1",
       "message": "Call the slow_job tool with minutes=10 and tell me what it returned"}' \
  >/dev/null; echo "client gone"
```

While it runs (no client connected):

- UI → `secondbrain` → workflow `$U/default/m5slow` → **Pending Activities**:
  `run_task`, attempt 1, *Last Heartbeat* refreshing about every 60 s,
  *Heartbeat Details* `{"last_seq": N}` with N growing as events are acked.
  Take a screenshot at ~5 minutes.
- CLI equivalent, twice a minute apart:

```sh
tq workflow describe -w "$W" | grep -E "ActivityType|Attempt|HeartbeatDetails|LastHeartbeatTime"
```

After ~10 minutes:

- history: `ActivityTaskCompleted` for `run_task` (`outcome: done`), then
  `deliver`; status `idle`; the UI shows the completed activity and no
  pending one;
- `jq -c 'select(.type=="tool_result")' agent-api/data/sessions/$U/default_m5slow/events/r0001/0*.jsonl`
  → the `slow_job finished after 10 min` result; the ticket says `done`;
- a client that reconnects gets the rest: `api '/api/v1/sessions/m5slow/events?after_seq=0' | jq length`
  (the current run's events after that seq).

## 3. Exit 3: kill agent-api mid-task, retry from the heartbeat `seq`

Use the slow tool again (2 minutes is enough) so the kill lands inside
`run_task`:

```sh
W=$U/default/m5kill
S=agent-api/data/sessions/$U/default_m5kill
MAX_TIME=20 chat '{"session_id": "m5kill", "input_id": "k-1",
       "message": "Call slow_job with minutes=2, then summarise the result in one sentence"}' \
  >/dev/null
sleep 75                                           # past the first 60 s heartbeat
tq workflow describe -w "$W" | grep -E "Attempt|HeartbeatDetails"    # attempt 1, {"last_seq": N}
docker compose kill -s SIGKILL agent-api           # the worker and the VM die with the container
docker compose up -d agent-api
```

The server notices the dead attempt after the heartbeat timeout (120 s) and
starts attempt 2 on the new worker (plus ~10 s for the dead worker's sticky
queue, spike):

```sh
tq workflow describe -w "$W" | grep -E "Attempt|HeartbeatDetails|LastFailure"
#   Attempt 2, LastFailure: activity Heartbeat timeout, HeartbeatDetails {"last_seq": N}
hostev $S workflow | grep activity_start
#   {"kind":"activity_start","activity":"run_task","input_id":"k-1","attempt":2,"resume":false,"floor_seq":N}
```

The re-attach is not a log line: it is that host event (`floor_seq` = the
previous attempt's last heartbeat `last_seq`, a floor only). `run_task`
then calls `start_task` with the same `input_id`: a task this process still
holds for it is re-attached, a settled one returns its stored settle, never
a second workflow task.

What the code does after a whole-process kill (read this before judging the
result): the VM died with the container and the new process has no
`vm/parked.json` for the session, so attempt 2's `ensure_ready()` boots a
**fresh** guest (`vm{op: restore, source: base, resume: false}`): a new run
`events/r0002/` with its own `session_start` at seq 1, and `k-1` is sent to
it as a new input. The task runs again from the start in r0002 (the model
calls `slow_job` a second time); nothing in r0001 is resumed, so there is no
`op_state{lost}` and no log return. The death path with `op_state{lost}`
(`hello{resume: true}`, M3 §5) applies only when the VM dies and the
agent-api process survives. A mid-flight MCP call is never re-attached on
either path (a fresh guest process cannot; `X-SB-Reattach` is only sent by
the same guest process after a park).

Pass, as built:

- the task finishes: `deliver(done)`, status `idle`, one ticket for `k-1`;
- `prepare_vm` ran once (the retry is the activity's, not the workflow's):
  `tq workflow show -w "$W" | grep -c ActivityTaskScheduled` counts
  prepare_vm, run_task, deliver = 3 (`run_task`'s two attempts are one
  scheduled activity);
- one workflow task, one settle across the session, each run clean:

```sh
for R in $S/events/r0*/; do
  echo "$R: inputs k-1 new: $(jq -s '[.[] | select(.type=="input" and .body.input_id=="k-1" and .body.dedupe=="new")] | length' $R/0*.jsonl)," \
       "session_start: $(jq -s '[.[] | select(.type=="session_start")] | length' $R/0*.jsonl)," \
       "settle: $(jq -s '[.[] | select(.type=="settle")] | length' $R/0*.jsonl)"
done
#   r0001: inputs k-1 new: 1, session_start: 1, settle: 0     (the killed run)
#   r0002: inputs k-1 new: 1, session_start: 1, settle: 1
docker compose exec agent-api python -m protocol.replay verify data/sessions/$U/default_m5kill
#   verified 1 target(s): 1 ok, 0 failed
```

**Known gap against exit criterion 3:** "no duplicate turn" holds for the
workflow (one task, one delivery, one ticket) and within the process
(`test_run_task_retry_reattaches_without_a_second_turn`), but not across a
process kill: the turn and its tool call run again in r0002, and r0002's
context does not hold r0001's conversation. Record the observed behaviour
in the M5 exit notes; closing it needs the worker to resume the
interrupted run (base restore + `hello{resume: true}` + log return) when a
new process finds an unsettled input in the current run.

## 4. Exit 4: the morning brief fires

Fire it now instead of waiting for 07:00:

```sh
tq schedule trigger -s default/daily/0
tq workflow list --query 'WorkflowType="ScheduleTickWorkflow"'
#   Completed  default/daily/0-2026-10-10T06:00:00Z  ScheduleTickWorkflow
W=$U/default/daily
tq workflow query -w "$W" --type status        # running, then idle with tasks_run +1
```

Pass:

- the session `daily` got a task of kind `schedule` with
  `input_id = default/daily/0-<fire time>` and text `Morning brief`
  (`tq workflow show -w "$W" --detailed | grep -A3 WorkflowExecutionSignaled`);
- the guest log of `daily` has the `input` and a `settle`; the ticket exists;
- a retried tick cannot double-run it: the `input_id` is the tick's workflow
  id, and the session drops a repeated one (`duplicate task ignored`);
- then the real one: the next morning,
  `tq schedule describe -s default/daily/0` shows `LastRunTime` at 07:00
  London and `ActionCounts.Total` +1, and the session has the brief.

Missed fires are skipped by design: stop `temporal` over 07:00 and start it
at 07:05 → no brief that day (`ActionCounts.MissedCatchupWindow` +1).

## 5. Things to watch

- `temporal.db` grows with history; 30-day retention deletes closed runs.
  `du -h agent-api/data/temporal/` weekly for the first month.
- Continue-as-new: `tq workflow describe -w "$W"` → `HistoryLength` stays
  well under 10 000; a hop shows as a new `RunId` with the same workflow id.
- Changing `answer_wait_s` / `wall_clock_s` in `tenants.json` needs an
  agent-api restart (the file is read once) and applies to workflows started
  afterwards (and to Workers created afterwards: a live Worker keeps its
  `answer_wait_s`); `tq workflow terminate -w "$W" --reason config`
  makes the next POST start a fresh one with the new config.
- If `temporal` is down, harness chat falls back to the direct path
  (`docs/FLOWS.md` section 1; `[host.temporal] ERROR: ... unreachable, direct path
  stays` at startup, or `Temporal worker exited (...), direct path from now
  on`); questions still park there (`answer_wait_s` on both paths), but no
  schedule fires and nothing survives an agent-api restart mid-task. Check
  `docker compose ps temporal`.

## 6. Checklist

| # | Criterion | Evidence | Done |
|---|---|---|---|
| 1 | question → parked 30 min → answer → done | history (timer, signal ~30 min later), status idle, ticket `done`, seq continuous | |
| 2 | 10-min MCP tool, no client, heartbeats | UI screenshot of Pending Activity heartbeats; completion in history; tool_result in log | |
| 3 | kill agent-api → retry from heartbeat seq, no duplicate turn | describe attempt 2 + details; `activity_start{attempt: 2, floor_seq}`; one ticket; per-run jq checks above (known gap: the turn re-runs in a new run) | |
| 4 | morning brief fires | ScheduleTickWorkflow run; `schedule`-kind input in `daily`; real 07:00 fire next day | |
