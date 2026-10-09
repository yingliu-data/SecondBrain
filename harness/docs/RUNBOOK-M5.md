# M5 runbook: Temporal, questions, schedules on the server

Follows `RUNBOOK-M3.md` (base snapshot, park and resume work on the box) and
the M4 gateway setup (`GATEWAY.md`). Verifies the M5 exit criteria of PLAN.md
section 13. Contracts: `docs/M5-CONTRACTS.md`; how the workflow works and the
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

Placeholders in «» are for the host and guest workstreams to fill in where
their names differ from the contract.

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

`agent-api/data/tenants.json`, default tenant (the one the phone uses):

```json
{
  "name": "default", "api_key": "<API_SECRET_KEY>", "is_default": true, "engine": "harness",
  "answer_wait_s": 86400,
  "schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily", "tz": "Europe/London"}]
}
```

Then `docker compose up -d agent-api` (compose sets `TEMPORAL_ADDRESS=temporal:7233`,
`TEMPORAL_NAMESPACE=secondbrain`). Check the startup:

```sh
docker compose logs agent-api | grep -iE "temporal|schedule"
#   «host: worker started on secondbrain-agent; ensure_schedules {'default/daily/0': 'created'}»
docker compose exec temporal temporal schedule list --namespace secondbrain
#   default/daily/0  {"Workflow":"ScheduleTickWorkflow"}  false  <next 07:00 London>
```

The UI from a laptop: `ssh -L 8233:localhost:8233 <box>`, then
http://localhost:8233/namespaces/secondbrain/workflows.

Shorthand for the steps below (`U` is the default tenant's `user`, the
directory under `agent-api/data/sessions/`):

```sh
U=owner
chat() { curl -sN -X POST localhost:8000/chat -H "Authorization: Bearer $API_SECRET_KEY" \
              -H 'Content-Type: application/json' -d "$1"; }
tq() { docker compose exec -T temporal temporal "$@" --namespace secondbrain; }
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

After the guest's question deadline («guest: the question deadline in
seconds when `answer_wait_s > 0`») the guest parks itself
(`parked{reason: "question", question_id}`), the worker snapshots it, and
`run_task` returns `parked`. Check:

```sh
QID=$(tq workflow query -w "$W" --type status -o json | jq -r '.queryResult[0].question_id')
echo "$QID"
tail -3 $S/events/0*.jsonl | jq -c '{seq, type, reason, question_id}'   # ... checkpoint, parked{reason:"question"}
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
- host events: `vm{op: restore, source: session}` and
  `workflow{kind: parked}` then `workflow{kind: delivered, outcome: done}`;
- guest log: the `input{kind: ui_response, question_id: <qid>, answer: "blue"}`
  continues at parked `seq` + 1, no second `session_start`, one `settle{outcome: done}`;
- ticket: `jq . $S/tickets/*.json` → `outcome: "done"`, `input_id: "q-1"`;
- `colour.txt` in the workspace says blue.

Negative checks (short): an `answer` signal with a wrong `question_id`
leaves it parked (`tq workflow signal -w "$W" --name answer --input '{"question_id":"nope","text":"x"}'`,
the agent-api log shows `answer for question 'nope' ignored`); repeating the
run with `"answer_wait_s": 120` on a test tenant delivers `escalated` after
two minutes, with a ticket and a guest `settle{outcome: escalated}`.

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

- `mcp-gateway/data/gateway.json`: upstream
  `"slow": {"url": "http://host.docker.internal:8799/mcp", "timeout_s": 900, "allow_private": true}`
  and `"slow"` in `tenants.default.upstreams`;
- `agent-api/data/tenants.json`: `"slow": {}` under `mcp_servers` (agent-api
  reads only the key) and, on the default tenant, `"wall_clock_s": 1200`
  (`run_task`'s start-to-close: the 600 s default would time the task out
  before the tool returns).
- «guest/host: any other per-call ceiling on the mcp side (guest operation
  timeout, forwarder read timeout) must be > 600 s; list them here».

```sh
docker compose restart mcp-gateway agent-api
W=$U/default/m5slow
chat '{"session_id": "m5slow", "input_id": "s-1",
       "message": "Call the slow_job tool with minutes=10 and tell me what it returned"}' \
  --max-time 20 >/dev/null; echo "client gone"
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
- `jq -c 'select(.type=="tool_result")' agent-api/data/sessions/$U/default_m5slow/events/0*.jsonl`
  → the `slow_job finished after 10 min` result; the ticket says `done`;
- a client that reconnects gets the rest: `curl -s "localhost:8000/sessions/m5slow/events?after_seq=0" -H "Authorization: Bearer $API_SECRET_KEY" | jq length`.

## 3. Exit 3: kill agent-api mid-task, retry from the heartbeat `seq`

Use the slow tool again (2 minutes is enough) so the kill lands inside
`run_task`:

```sh
W=$U/default/m5kill
S=agent-api/data/sessions/$U/default_m5kill
chat '{"session_id": "m5kill", "input_id": "k-1",
       "message": "Call slow_job with minutes=2, then summarise the result in one sentence"}' \
  --max-time 20 >/dev/null
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
docker compose logs agent-api | grep -iE "run_task|attempt|last_seq"
#   «host: the line that logs re-attach by input_id from the last_seq floor»
```

Pass:

- the task finishes: `deliver(done)`, status `idle`, one ticket for `k-1`;
- `prepare_vm` ran once (the retry is the activity's, not the workflow's):
  `tq workflow show -w "$W" | grep -c ActivityTaskScheduled` counts
  prepare_vm, run_task, deliver = 3;
- no duplicate turn, no second task:

```sh
E="$S/events/0*.jsonl"
jq -s '[.[] | select(.type=="input" and .input_id=="k-1" and .dedupe=="new")] | length' $E   # 1
jq -s '[.[] | select(.type=="turn_start") | .turn_id] | (length == (unique|length))' $E    # true
jq -s '[.[] | select(.type=="turn_start")] | length' $E    # == number of llm_response events:
jq -s '[.[] | select(.type=="llm_response")] | length' $E
jq -s '[.[] | select(.type=="session_start")] | length' $E  # 1
jq -r 'select(.seq) | .seq' $E | python3 -c "import sys; s=[int(l) for l in sys.stdin]; print('gaps:', [(a, b) for a, b in zip(s, s[1:]) if b != a + 1])"
#   gaps: []
```

- the interrupted MCP call shows the M3 kill path: `op_state{state: lost}`
  and a `tool_result` error, then the model's next turn (or the gateway's
  result if the call was re-attached; «host: which one M5 does»); the guest
  resumed at the acked `seq` + 1, which is ≥ the heartbeat `last_seq`.

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
- Changing `answer_wait_s` / `wall_clock_s` in `tenants.json` applies to
  workflows started afterwards; `tq workflow terminate -w "$W" --reason config`
  makes the next POST start a fresh one with the new config.
- If `temporal` is down, harness chat falls back to the direct path
  (M5-CONTRACTS §1) and no question parks; check `docker compose ps temporal`.

## 6. Checklist

| # | Criterion | Evidence | Done |
|---|---|---|---|
| 1 | question → parked 30 min → answer → done | history (timer, signal ~30 min later), status idle, ticket `done`, seq continuous | |
| 2 | 10-min MCP tool, no client, heartbeats | UI screenshot of Pending Activity heartbeats; completion in history; tool_result in log | |
| 3 | kill agent-api → retry from heartbeat seq, no duplicate turn | describe attempt 2 + details; jq checks above | |
| 4 | morning brief fires | ScheduleTickWorkflow run; `schedule`-kind input in `daily`; real 07:00 fire next day | |
