# Async tool calls — the M6 measurement (PLAN.md D6)

D6: the coordinator is built for async tool calls (a turn may start on
`[pending]` placeholders while operations run), but `async_tools = False` is
the default until Qwen3-14B is shown to cope with placeholders. M6 measures it
on a fixed task set (docs/M6-CONTRACTS.md section 5). The default stays
`False` unless the finding below says otherwise.

## What async mode does

With `hello.config.async_tools: true` (docs/GUEST.md "Async mode" has the
exact rules) the guest no longer waits for every call of a response before
waking the model:

- after a response with tool calls, results are batched for `limits.grace_s`
  (results already queued always share a turn); when the window closes with
  some results in and some calls still running, the model is woken
  (`wake{grace}`) and sees each running call as a `[pending]` tool message;
  after the window each result wakes it (`wake{tool_result}`), and a new
  input always does (`wake{input}`);
- the model may call more tools meanwhile, or end its turn without calls to
  wait; such a turn does not settle while calls run;
- a result whose `[pending]` the model already saw is `tool_result{late:
  true}` and arrives in the next turn as a user message
  `[late tool result] call_id: <id>, tool: <name>` + the content; the prefix
  is never rewritten;
- the system block gains a fixed `## Tool calls` paragraph explaining this.

Every async log folds, verifies its checkpoints and reproduces each turn's
`request_hash` (harness/tests/test_guest_async.py).

With tools that all finish inside the grace window, async mode logs exactly
what sync mode logs (one wake for all results). It only differs when one
lookup outlasts the window, which is why the task set has `slow_report`
(a script that sleeps 3 s, above the default `grace_s` of 2 s).

## The bench (`harness/bench/`)

`python -m bench.async_tools` runs every task in `bench/tasks.json` through
the real guest (`python -m guest.main` as a subprocess, as the e2e tests do)
against the fake host (`tests/fakes/fake_host.py`) over Unix-socket doubles of
the vsock ports. Each task runs twice per `--runs`: `async_tools` off, then on.
The workspace is seeded fresh from `tasks.json` for every run.

The task set has 9 tasks. Each one needs two or three independent lookups
(`read`, `grep`, `bash`) over a small seeded project (`src/`, `data/*.csv`,
`logs/server.log`, `docs/notes.md`, `README.md`, `scripts/report.sh`).
`slow_report` pairs a 3 s script with a fast read, so a real model's `on` run
shows a placeholder there; the other tasks' lookups are fast. A checker
(`contains_all`: whole-token, case-insensitive) is applied to the settle
answer.

Per run it reports: pass/fail (checker + `settle{done}` + the log replays
clean), turns, prompt/completion tokens (summed `llm_response.usage`), tool
calls, `par` (the most calls in one response), `ph` (placeholders committed:
calls the model saw as `[pending]`), and wall time (task sent → `settle`
delivered). The JSON rows and totals also carry `late_results` (late fills).

How to read the table: `ph` > 0 in an `on` row means async mode took effect
in that run; `ph` is always 0 in `off` rows. Each placeholder costs a turn
whenever the model ends its turn to wait (a turn on placeholders, then one
per late result), so `turns` and prompt tokens go up with `ph`; the gain to
look for is wall time (the model works while a slow call runs) and whether
answers stay correct, i.e. the model neither ignored a `[pending]` result nor
answered from it. Compare totals, not single rows. Then it reports totals per mode. With
`--json` / `--out` the full rows go to JSON, including each answer and the
values the checker missed.

### CI

`--fake` scripts the LLM from each task's `fake` entry: one response with the
task's parallel calls plus a slow `bash` call (`sleep 0.75`), then per
request "waiting" (no calls) while the request still shows a `[pending]`
call without its late result, else the answer. `grace_s` is 0.25 in fake
mode (`--grace-s` overrides it), so every `on` row gets at least one
placeholder: `off` rows take 2 turns, `on` rows 2 + `ph` (3, and 4 for
`slow_report`). The tools still really run in the guest. In
fake mode a run passes only if the tool results themselves contain the
checker's values and no tool returned an error, which checks that the seed,
the tools and the task agree. `tests/test_bench_async_tools.py` runs it (and
the `--llm-url` path against a local OpenAI-compatible double) in
`uv run pytest`.

### On the server (vLLM, Qwen3-14B)

vLLM is `secondbrain-llm:8080` on the compose `llm_internal` network (no
published port) and serves the model as `qwen3-14b`. Run the bench from a
container on that network; the exact command is in `RUNBOOK-M6.md` §4:

```
cd harness
uv run python -m bench.async_tools \
    --llm-url http://secondbrain-llm:8080 --model qwen3-14b \
    --runs 3 --out /tmp/async-bench-$(date +%F).json
```

- `--llm-url` is the OpenAI-compatible base URL. The bench POSTs the guest's
  request to `<url>/v1/chat/completions`, non-streamed (`params.stream:
  false`, temperature 0, thinking off as the provider always sends). It takes
  the role of the host's LLM proxy. An unreachable upstream answers 502, so
  the guest provider's retries apply. `--api-key` (or `LLM_API_KEY`) adds a
  bearer token.
- `--model` is the served model name (what `GET <url>/v1/models` lists).
- `--only csv_rows,todos` runs a subset. `--wall-clock-s` (default 180) is the
  guest's per-task limit. `--grace-s` (default 2.0) is `limits.grace_s`; a
  lower value makes placeholders likelier with fast tools. `--work-dir DIR --keep` keeps every run's directory
  (the guest log under `log/events/`, the workspace under `ws/`), so a run can
  be inspected with `python -m protocol.replay replay DIR/<run>/log`.
- The exit code is 0 only when every run passed.

Run it with nothing else using vLLM, so the wall times are comparable.
Record the date, the vLLM version and the model revision.

## Results

To be filled in on the server (async mode is implemented in the guest; a
finding needs `ph` > 0 in the `on` rows, at least for `slow_report`). Use
totals over `--runs 3`.

| Date | vLLM / model | Mode | Passed | Turns | Prompt tok | Completion tok | Tool calls | Placeholders | Wall s |
|---|---|---|---|---|---|---|---|---|---|
| | qwen3-14b | off | /27 | | | | | 0 | |
| | qwen3-14b | on | /27 | | | | | | |

Per-task notes (failures, answers that ignored a `[pending]` result, extra
turns):

| Task | off | on | Note |
|---|---|---|---|
| csv_rows | | | |
| log_levels | | | |
| version_mismatch | | | |
| todos | | | |
| functions | | | |
| port_mismatch | | | |
| owner_and_user5 | | | |
| line_counts | | | |
| slow_report | | | |

**Finding:** _pending._ Until it is recorded, `async_tools = False` stays the
default (D6).
