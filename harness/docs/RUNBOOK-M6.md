# M6 runbook: replay, legacy sessions, all tenants on harness, async measurement

Follows `RUNBOOK-M5.md` (Temporal, questions and schedules work on the box).
Verifies the M6 exit criteria of PLAN.md section 13 on the server:
every log replays, the legacy sessions are event logs, every tenant runs on
`harness`, and the async tool-call finding is recorded. Contracts:
`docs/M6-CONTRACTS.md`; the replay CLI and the invariants: `docs/REPLAY.md`;
the async mode and the bench: `docs/ASYNC-TOOLS.md`, `docs/GUEST.md`
"Async mode".

| Step | What | Code |
|---|---|---|
| §1 | verify every session on the box | `harness/protocol/replay.py` (`python -m protocol.replay`) |
| §2 | legacy sessions → event logs, and they continue | `agent-api/app/session/migrate.py` (`python -m app.session.migrate`), `StateSync.retool_migrated`, `Worker._boot` |
| §3 | every tenant on `harness`, tenants.json and gateway.json | `agent-api/app/tenants/models.py` (`RETIRED`), `app/tenants/registry.py`, `app/session/factory.py`, `mcp-gateway/app/migrate_config.py` |
| §4 | async off/on measurement, the default | `harness/bench/async_tools.py`, `agent-api/app/host/worker.py` `build_hello` |

Paths: the compose project root is the working directory; inside the
agent-api container the working directory is `/root/agent-api`, so the
sessions root is `data/sessions` there (`SESSIONS_ROOT`) and
`agent-api/data/sessions` on the host. `protocol` and `app` are importable
by the container's `python` (the venv is on `PATH`).

Shell helpers: `api`, `chat` and `hostev` from RUNBOOK-M5 §0 (requests run
inside the agent-api container: it publishes no host port, has no `curl`,
and `/api/v1/*` wants Bearer + HMAC; `KEY=<api_key>` selects another
tenant), `llm` from RUNBOOK-M2.

Order on the box: deploy the M6 image, §3 (config) before the restart, §2
(migration) right after it and before users reopen old sessions, §1, then §4
whenever the GPU is free.

## 0. Before deploying (once)

```sh
cd ~/SecondBrain
tar czf ~/sessions-pre-m6-$(date +%F).tgz -C agent-api/data sessions tenants.json \
    $( [ -f agent-api/data/conversations.db ] && echo conversations.db )
cp mcp-gateway/data/gateway.json ~/gateway-pre-m6-$(date +%F).json 2>/dev/null || true
docker compose exec agent-api printenv SESSION_BACKEND    # empty or "dir" (§3)
```

## 1. Verify every session on the box

```sh
docker compose exec agent-api python -m protocol.replay verify --all data/sessions; echo "exit $?"
#   verified 214 target(s): 214 ok, 0 failed          (stderr)
#   exit 0
```

On the host instead (a checkout with python ≥ 3.12; `protocol` is stdlib
only): `cd harness && python3 -m protocol.replay verify --all ../agent-api/data/sessions`.

What it does (`verify_session` per target): `--all` takes every directory
under the root holding an `events/` directory with runs, segments or a host
log (`find_sessions`); legacy sessions with only `history.jsonl` are not
targets until §2 converts them. Per run (`events/rNNNN/`): every line is a
JSON object, `validate_log`, the fold, every checkpoint's `state_hash` and
the `prev_hash` chain; across runs one `session_id`; the host log
(`events/host-NNNNNN.jsonl`): `hseq` gapless from 1, `type` a string, `body`
an object. With `HARNESS_SANDBOX=local` the guest's own buffer
`{session}/guest/` is found too and verified as a flat log (its run is named
`events`); that is expected.

Output: nothing on stdout for clean targets; one line per problem,
`<target>: <problem>`, then the count on stderr; exit 1 when any target
failed (`no session found` on stderr when the root has none). `--json`
prints `{"ok", "targets", "failed", "results": [{"target", "problems"}]}`:

```sh
docker compose exec -T agent-api python -m protocol.replay verify --all --json data/sessions \
  | jq -r '.results[] | select(.problems != []) | .target'      # the failed sessions
docker compose exec agent-api python -m protocol.replay replay data/sessions/<user>/<tenant>_<sid>
#   data/sessions/...: r0001: session <sid> seq 1..N turns=.. calls=.. open=.. settle=done parked=False state_hash=...
```

What a failure means, by its text:

| Problem | Meaning | Action |
|---|---|---|
| `r0001: seq N: torn last line (000003.jsonl:L: ...)` | the process died in the middle of appending event N. The host acks only after the append and fsync, so N was never acked. A torn line also stops `StateSync` loading the session (the session is unusable until fixed) | keep a copy of the segment, cut the partial last line (`truncate -s <size up to the last \n>`), verify again; the guest re-sends unacked events on reconnect, or the run resumes without it |
| `r0001: seq N: line is not JSON` / `not a JSON object` (not the last line) | corruption or a manual edit | restore that session from the backup; do not hand-edit logs |
| `r0001: seq N: <field>...` (from `validate_log` / the fold: a schema error, `seq` gap or duplicate, a `FoldError`) | the log breaks a protocol rule: a host or guest bug, or a log written by a newer/older protocol | keep the session dir as evidence, file it with the `replay` output; the session will fail to load (`run N does not fold`) |
| `r0001: seq N: checkpoint state_hash mismatch: fold through seq T gives ..., log says ...` / `checkpoint.prev_hash ... does not continue the chain` | the fold today does not reproduce what the guest folded when it parked: an edited log, or a fold rule change that moves hashes (REPLAY.md: such a change must ship with regenerated goldens; on production logs it means parked sessions written before it no longer verify) | if many sessions fail on the same seq type right after a deploy, suspect the deploy; otherwise treat as tampering |
| `r0002: seq 1: session_id ... != the session's ...` | two sessions' runs in one directory | investigate by hand |
| `r0001: run directory holds no segments` | an empty `rNNNN/` | remove the empty directory after checking nothing else is in it |
| `host: hseq N: expected hseq N (gapless from 1), got ...` | the host log lost or duplicated a line (two writers, or a crash) | the host log is diagnostics, not state: note it; the session still loads |

Pass: exit 0, i.e. `verified N target(s): N ok, 0 failed`. Re-run after §2
(the migrated runs are new targets).

## 2. Legacy sessions → event logs

Two kinds of legacy data; both end as an event log run `events/r0000/`:

- `data/conversations.db` rows (the sqlite store, `SESSION_DB_PATH`): only if
  that file exists. `python -m app.session.migrate` (no `--events`) creates a
  session directory per row with its run `r0000`, built the same way;
  existing session directories are skipped; SQLite is untouched.
- session directories with a non-empty `history.jsonl` and no run under
  `events/`: `python -m app.session.migrate --events <root>`.

Run the migration after the M6 deploy and before old sessions are reopened:
a legacy session that gets a message first starts a fresh run `r0001`
without its history, and the migration then skips it (`skipped_existing`).

### 2a. Dry run

```sh
docker compose exec agent-api python -m app.session.migrate --events --dry-run data/sessions
#     default/default_abc: would_migrate {"exchanges": 12, "replies": 12, "tool_calls": 3, "unanswered": 0, "skipped_rows": 0, "events": 97}
#     wcc-event/wcc-event_x1: failed {"problems": ["seq 14: ..."], ...}
#   Done (dry run): {'would_migrate': 40, 'skipped_existing': 170, 'skipped_empty': 4}
```

Statuses: `would_migrate` / `migrated`, `skipped_existing` (a run under
`events/` already; silent), `skipped_empty` (no or empty `history.jsonl`;
silent), `skipped_invalid` (session.json unreadable or an unsafe
user/tenant/session id), `failed` (the built run does not pass
`protocol.fold.replay`, or an exception; `problems` lists up to five).
Counts: `exchanges` (user messages), `replies`, `tool_calls` (from the
history's `tool_calls` rows, else the legacy `logs/trace.jsonl` of the same
ticket), `unanswered` (a user message that never got a reply: its settle is
`failed` with an empty answer), `skipped_rows` (assistant rows before the
first user message), `events`. Exit 1 when any session failed. Nothing is
written.

### 2b. For real

With agent-api stopped, so no worker opens a session in between:

```sh
docker compose stop agent-api
# only if agent-api/data/conversations.db exists:
docker compose run --rm --no-deps agent-api python -m app.session.migrate
#     'abc' -> default/default_abc (8 msgs)
#   Done: {'migrated': 3, 'skipped_existing': 0, 'skipped_invalid': 0, 'failed': 0}
docker compose run --rm --no-deps agent-api python -m app.session.migrate --events data/sessions
#     default/default_abc: migrated {...}
#   Done: {'migrated': 40, 'skipped_existing': 170, 'skipped_empty': 4}
docker compose run --rm --no-deps agent-api python -m protocol.replay verify --all data/sessions
#   verified 254 target(s): 254 ok, 0 failed
docker compose up -d agent-api
```

Idempotent: a second `--events` pass reports every converted session as
`skipped_existing` (silent) and `migrated` nowhere. Each run is written
through a temporary directory renamed to `events/r0000`, so an interrupted
pass leaves whole runs or none and the next pass finishes it. The legacy
files (`history.jsonl`, `logs/trace.jsonl`) stay in place, unread. Undo for
one session: remove its `events/r0000/` (before it was continued).

`failed` sessions are left as they were (no run); look at the `problems`,
fix the source row by hand if it is obviously broken, or leave the session
legacy (its history is then not in context when it is next used).

### 2c. A migrated session continues

Pick one migrated session (`S=agent-api/data/sessions/<user>/<tenant>_<sid>`)
and look at its run before using it:

```sh
jq -c 'select(.type=="session_start") | {migrated: .body.migrated, resumed: .body.resumed,
       tools: [.body.tools[].name]}' $S/events/r0000/000001.jsonl
#   {"migrated":true,"resumed":false,"tools":["web_search"]}    (the legacy tools its calls named; [] when none)
tail -1 $S/events/r0000/0*.jsonl | jq -c '{seq, type}'          # {"seq":97,"type":"checkpoint"}
```

Then send it a message as its tenant (the `api`/`chat` helpers of
RUNBOOK-M5 §0; `KEY=<that tenant's api_key>` for a non-default tenant):

```sh
KEY=<tenant key> chat '{"session_id": "<sid>", "input_id": "mig-1",
                        "message": "What did we talk about last time?"}'
```

Pass:

- the first boot is a resume: host event `vm{op: restore, source: base,
  resume: true}` (`hostev $S vm`), not `resume: false`;
- the tools were re-pinned before the hello: `session_start.tools` in
  `r0000` now lists the hello's effective tools
  (`protocol.allowlist.effective_tools`: the guest built-ins, the host and
  device tools, the gateway's mcp entries) and still the legacy ones; the
  run's closing checkpoint was recomputed;
- no `events/r0001/`: the guest took the log return and continued `r0000`
  at the migration's last seq + 1 (`clock{kind: host_hello}` and the new
  `input` follow the checkpoint, no second `session_start`); on Firecracker
  the console shows `hello for session <sid> (<user>/<tenant>) resume` and
  `log return: N events through seq N`;
- the answer refers to the old conversation;
- `docker compose exec agent-api python -m protocol.replay verify data/sessions/<user>/<tenant>_<sid>` is clean.

```sh
jq -c 'select(.type=="session_start") | [.body.tools[].name]' $S/events/r0000/000001.jsonl
ls $S/events/                                   # host-000001.jsonl  r0000   (no r0001)
docker compose logs agent-api | grep 'keeps its tools'    # must be empty
```

A `migrated run .../r0000 keeps its tools: ...` warning means the re-pin
failed and the run kept only its legacy tools (the guest can then call only
those in that session); file it with the session's `replay` output.

The legacy `workspace/` files of a migrated session are not in the guest's
workspace (the VM mounts its own `vm/workspace.img`; the local sandbox uses
the private `vm/workspace/`).

## 3. Every tenant on `harness`

What changed for the server (M6-CONTRACTS §3):

- `AGENT_ENGINE`, the legacy loop and `app/agent/` are gone. A tenant's
  `engine` key is accepted and dropped with a warning; so are `mcp_servers`
  (the tenant list) and `promoted`. A tenant that lists `mcp_servers` and has
  no `local_skills` gets `local_skills: []` (it never had local skills). The
  top-level `mcp_servers` map is ignored by agent-api without a warning (it
  is the gateway's source).
- MCP tools come only from the mcp-gateway (`gateway.json`); agent-api asks
  it for a session's tools at the first boot (`GET /internal/tools`).
- The guest avatar page (`POST /api/v1/guest/chat`) runs on the same harness
  path as the built-in `guest` tenant: `auth: origin`, `local_skills:
  ["avatar_control"]`, no guest built-ins, `max_tools` 2, `answer_wait_s` 0,
  the page's origins. A tenants.json entry `{"name": "guest", "auth":
  "origin", ...}` replaces it. `auth: origin` is refused on any other tenant,
  and an entry named `guest` must use it (startup error).
- `SESSION_BACKEND` must be `dir` (the default); anything else stops
  agent-api at startup.

### 3a. gateway.json first

`migrate_config` reads the `mcp_servers` map and the tenant lists from
tenants.json, so generate gateway.json before removing them:

```sh
cd mcp-gateway
uv run python -m app.migrate_config ../agent-api/data/tenants.json > data/gateway.json
chmod 600 data/gateway.json && cd ..
jq '{upstreams: (.upstreams | keys), tenants: (.tenants | map_values(.upstreams))}' mcp-gateway/data/gateway.json
```

The default tenant (`is_default: true`, or the entry whose `api_key` equals
`API_SECRET_KEY` in the environment you run it from) gets every upstream;
each other tenant its `mcp_servers` list with `allow: ["*"]`, its
`promoted` list and `max_concurrent: 2`. Without `data/gateway.json` the
gateway starts with no upstreams (`gateway config /data/gateway.json not
found; no upstreams, every tenant default-denied`) and every session starts
without MCP tools.

### 3b. tenants.json

Remove `engine` from every entry (or leave it: it is only a warning). The
`mcp_servers` / `promoted` keys can stay as the source for the next
`migrate_config` run (one warning per tenant per start), or move to a copy
you keep for that. Check the guest: no entry named `guest` unless it is
`auth: "origin"`, and no other entry with `auth: "origin"`.

```sh
jq -r '.tenants[] | [.name, (.engine // "-"), ((.mcp_servers // []) | join(",")), (.auth // "key")] | @tsv' agent-api/data/tenants.json
docker compose restart mcp-gateway agent-api
docker compose logs agent-api | grep -E '\[tenants\]'
#   ... [tenants] WARNING: tenant default: `engine: harness` ignored (every tenant runs the harness engine)
#   ... [tenants] WARNING: tenant wcc-event: `mcp_servers: ['wcc-events']` ignored (the mcp-gateway's gateway.json owns MCP access)
#   ... [tenants] INFO: Tenants loaded: ['default', 'wcc-event', 'wcc-mentorship', 'wcc-analytic', 'guest']
docker compose ps agent-api                                 # running, not restarting
```

A crash loop instead: `docker compose logs agent-api | tail -30`. Known
startup errors: `SESSION_BACKEND='sqlite' is not supported: the harness
engine needs the directory session store (SESSION_BACKEND=dir, the
default)...` (remove the variable), `tenant 'x': auth: origin is for the
'guest' tenant only (and it must use it)`, `Duplicate api_key (tenant x)`.

### 3c. Each tenant answers through the harness

For each tenant with a key (and the guest page):

```sh
KEY=<tenant api_key> chat '{"session_id": "m6-smoke", "input_id": "m6-1", "message": "Which tools do you have? One line."}'
ls agent-api/data/sessions/<user>/<tenant>_m6-smoke/events/     # host-000001.jsonl  r0001
hostev agent-api/data/sessions/<user>/<tenant>_m6-smoke vm      # restore (or boot) of this session
jq -c 'select(.type=="session_start") | [.body.tools[].name]' \
   agent-api/data/sessions/<user>/<tenant>_m6-smoke/events/r0001/000001.jsonl
#   the tenant's local skills, its promoted MCP tools, search_tools/describe_tool/call_tool when it has upstreams
docker compose logs agent-api | grep 'starting without MCP tools'   # must be empty
docker compose exec -T agent-api python - <<'EOF'
import json, urllib.request
req = urllib.request.Request("http://localhost:8000/api/v1/guest/chat", method="POST",
    data=json.dumps({"session_id": "m6guest", "message": "wave"}).encode(),
    headers={"Content-Type": "application/json", "Origin": "https://robot.yingliu.site"})
print(urllib.request.urlopen(req).read().decode()[:400])     # event: avatar_command ... event: done
EOF
ls agent-api/data/sessions/guest/                           # guest_m6guest
```

Pass: every tenant's smoke session has `events/r0001` with a `settle`; MCP
tenants list the gateway's tools; the guest page answers with
`avatar_command`; `verify --all` (§1) stays clean.

## 4. The async tool-call measurement

`async_tools` is off by default and stays off unless this measurement says
otherwise (PLAN.md D6, `docs/ASYNC-TOOLS.md`). The default lives in one
place on the host: the literal `"async_tools": False` in
`Worker.build_hello` (`agent-api/app/host/worker.py`); the guest reads
`hello.config.async_tools` (default false, `guest/config.py`). There is no
environment variable or tenant key for it.

### 4a. Run the bench against the vLLM container

The bench is stdlib-only Python: it runs the real guest
(`python -m guest.main`) against the fake host from `harness/tests/fakes/`
and forwards the guest's LLM requests to vLLM's `/v1/chat/completions`
(non-streamed, temperature 0, thinking off). It needs the `harness/`
checkout (the agent-api image does not carry `bench/` or `tests/`) and
network access to `secondbrain-llm:8080`, which is only on the internal
`llm_internal` network and not published. Run it in a throwaway container on
that network (no internet needed):

```sh
docker compose --profile local-llm up -d llm && docker compose ps llm       # (healthy)
NET=$(docker network ls --format '{{.Name}}' | grep '_llm_internal$')
mkdir -p ~/async-bench
bench() { docker run --rm --network "$NET" -e PYTHONDONTWRITEBYTECODE=1 \
            -v "$PWD/harness:/harness:ro" -v ~/async-bench:/out -w /harness \
            python:3.12-slim python -m bench.async_tools "$@"; }
bench --llm-url http://secondbrain-llm:8080 --model qwen3-14b --only csv_rows     # smoke: 2 rows, PASS
bench --llm-url http://secondbrain-llm:8080 --model qwen3-14b --runs 3 \
      --out /out/async-bench-$(date +%F).json | tee ~/async-bench/async-bench-$(date +%F).txt
```

`--model` is the served model name (`--served-model-name=qwen3-14b` in
compose; `llm /v1/models` of RUNBOOK-M2 lists it). `--llm-url` is the base
URL without `/v1`. Defaults: `--grace-s 2.0` (`limits.grace_s`),
`--wall-clock-s 180` per task; `--work-dir /out/runs --keep` keeps every
run's directory for §4c. Progress goes to stderr
(`[1] csv_rows async=off: PASS (6.3s)`), the table and the totals to stdout:

```
            task   async    pass   turns  prompt   compl   calls     par      ph  wall_s
        csv_rows     off    PASS       2    1830      95       2       2       0    6.31
        csv_rows      on    PASS       2    1912      95       2       2       0    6.40
     slow_report      on    PASS       4    3010     140       2       2       1    9.80
...
total async=off: 27/27 passed, turns 54, tokens 50000+2600, tool calls 60, placeholders 0, wall 170.00s
total async=on: 26/27 passed, turns 61, tokens 58000+2900, tool calls 61, placeholders 5, wall 150.00s
```

(numbers illustrative). Three runs = 9 tasks × 3 = 27 rows per mode, the
`/27` of the results table. Exit 0 only when every run passed; a FAIL row
is data, not a reason to stop. Run it with nothing else using vLLM, so wall
times compare. Record the vLLM version (`docker compose images llm`:
`vllm/vllm-openai:v0.11.0`) and the model (`Qwen/Qwen3-14B-AWQ` served as
`qwen3-14b`).

If the host can reach the container's IP on the internal bridge
(`curl -s http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' secondbrain-llm):8080/v1/models`),
`cd harness && python3 -m bench.async_tools --llm-url http://<that ip>:8080 --model qwen3-14b --runs 3 ...`
works too (python ≥ 3.12, no dependencies).

### 4b. Fill in `docs/ASYNC-TOOLS.md`

From the `total async=off` / `total async=on` lines (or `.totals.off` /
`.totals.on` in the JSON) into the Results table, one row per mode:

| Column | Source |
|---|---|
| Date | the run date |
| vLLM / model | `v0.11.0 / Qwen3-14B-AWQ (qwen3-14b)` |
| Passed | `passed`/27 |
| Turns, Prompt tok, Completion tok, Tool calls | `turns`, `prompt_tokens`, `completion_tokens`, `tool_calls` |
| Placeholders | `placeholders` (0 for off) |
| Wall s | `wall_s` |

```sh
jq -r '.totals | to_entries[] | [.key, "\(.value.passed)/\(.value.runs)", .value.turns, .value.prompt_tokens,
       .value.completion_tokens, .value.tool_calls, .value.placeholders, .value.late_results, .value.wall_s] | @tsv' \
   ~/async-bench/async-bench-$(date +%F).json
jq -r '.rows | group_by(.task)[] | [.[0].task,
       ([.[] | select(.async_tools | not) | .pass] | map(if . then "P" else "F" end) | join("")),
       ([.[] | select(.async_tools) | .pass] | map(if . then "P" else "F" end) | join("")),
       ([.[] | select(.async_tools) | .placeholders] | add)] | @tsv' ~/async-bench/async-bench-$(date +%F).json
#   task  off(3 runs)  on(3 runs)  placeholders in on rows   -> the per-task table
jq -r '.rows[] | select(.pass | not) | [.task, (if .async_tools then "on" else "off" end), .outcome, (.missing | join(",")), .answer] | @tsv' \
   ~/async-bench/async-bench-$(date +%F).json             # the notes column: what each failure missed
```

The measurement only counts if async mode took effect: `ph` > 0 in the `on`
rows, at least for `slow_report` (its 3 s script outlasts the 2 s grace
window). If every `on` row has `ph` 0, the model answered all calls inside
the window: re-run with `--grace-s 0.5` and say so in the doc.

### 4c. The open question: late results as user messages on Qwen3/vLLM

A result that arrives after the model saw its `[pending]` placeholder is
shown in the next turn as a **user** message
`[late tool result] call_id: <id>, tool: <name>\n<content>` (never a second
`tool` message; `docs/GUEST.md` "Async mode"). Whether Qwen3-14B under
vLLM's hermes template reads that as the tool's answer is what to check:

- vLLM accepts the shape: no `on` row with `outcome` null or `failed` and a
  provider error (`.rows[] | select(.outcome != "done") | .stderr_tail`);
- the model uses the late value: `slow_report` `on` rows with
  `late_results` > 0 pass (the answer has `42`);
- it does not answer from the placeholder (an answer without `42` written
  while the call showed `[pending]`) and does not call the same tool again
  for it (`tool_calls` of `on` ≈ `off` for that task);
- it waits sensibly: a turn that ends without calls while a call runs is
  followed by the late result's turn, not by a second "waiting" turn.

Look at a kept run (`--work-dir /out/runs --keep`):

```sh
D=$(grep -l '"late":true' ~/async-bench/runs/ab-*/log/events/*.jsonl | xargs grep -l 'in-slow_report' | head -1 | xargs dirname | xargs dirname)
PYTHONPATH=harness python3 - "$D" <<'EOF'
import sys
from protocol.fold import replay
from protocol.replay import load_session
[events] = load_session(sys.argv[1])
state, problems = replay(events)
print("problems:", problems or "none")
msgs = state.committed_prefix
for i, m in enumerate(msgs):
    if m["role"] == "user" and str(m.get("content", "")).startswith("[late tool result]"):
        print(m["content"][:200]); print("->", msgs[i + 1] if i + 1 < len(msgs) else state.last_settle)
EOF
```

Record the answer (yes / no / partly, with the row that shows it) under
"Finding" in `docs/ASYNC-TOOLS.md`.

### 4d. Flip `async_tools` on? The criteria

Turn it on only if all hold over the 27-run totals:

1. async took effect: `placeholders` > 0 in `on` (at least every
   `slow_report` run);
2. no correctness loss: `on` passed ≥ `off` passed, and no `on` failure is
   caused by a placeholder or a late result (§4c);
3. a real gain: `on` total `wall_s` clearly below `off` (the point of the
   mode: the model works while a slow call runs), at an acceptable cost in
   turns and prompt tokens (each placeholder costs a turn when the model
   waits);
4. §4c answered yes: late results are read as tool output.

Otherwise the Finding says why and the default stays `False`. Flipping is a
code change: `"async_tools": True` in `Worker.build_hello`
(`agent-api/app/host/worker.py`), the Finding in `docs/ASYNC-TOOLS.md` and
D6 in PLAN.md, then a normal deploy. The flag is part of `config_hash` and
adds the `## Tool calls` section to the system block, so it applies to the
next hello: fresh sessions and every resume after the deploy. Prefer a
deploy while no task is running.

## 5. Checklist

| # | Criterion | Evidence | Done |
|---|---|---|---|
| 1 | every session on the box verifies | `verify --all data/sessions`: `N ok, 0 failed`, before and after §2 | |
| 2 | legacy sessions migrated and continuing | `--events --dry-run` then real: `failed` 0 (or listed); one migrated session resumed (`vm{resume: true}`, re-pinned tools, no `r0001`, verify clean) | |
| 3 | all tenants on harness | `[tenants]` warnings only for retired keys; gateway.json generated; each tenant's smoke session settles; guest page OK | |
| 4 | async finding recorded | `--runs 3` totals and per-task table in ASYNC-TOOLS.md; §4c answered; default decided (stays `False` unless §4d holds) | |
