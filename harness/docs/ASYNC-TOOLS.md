# Async tool calls — the M6 measurement (PLAN.md D6)

D6: the coordinator is built for async tool calls (a turn may start on
`[pending]` placeholders while operations run), but `async_tools = False` is
the default until Qwen3-14B is shown to cope with placeholders. M6 measures it
on a fixed task set (docs/M6-CONTRACTS.md section 5). The default stays
`False` unless the finding below says otherwise.

## Status of async mode in the guest (read this first)

As of M6 the guest coordinator is **sync-only** (`guest/coordinator.py`
docstring: "Sync mode only (M1)"). `config.async_tools` is parsed and carried in
the hello and the config hash, and the grace timer is recorded
(`clock{grace_expiry}`), but no turn starts on placeholders. The fold already
models placeholders and late fills (docs/REPLAY.md invariants 2 and 5), so
replay is ready for it.

So a bench run today measures the sync path twice. The `ph` column
(placeholders committed in the run, from the fold) shows whether async mode
took effect: it is 0 for every run until the coordinator implements it. A
finding must not be recorded from runs where `ph` is 0 in the `on` rows.

## The bench (`harness/bench/`)

`python -m bench.async_tools` runs every task in `bench/tasks.json` through
the real guest (`python -m guest.main` as a subprocess, as the e2e tests do)
against the fake host (`tests/fakes/fake_host.py`) over Unix-socket doubles of
the vsock ports. Each task runs twice per `--runs`: `async_tools` off, then on.
The workspace is seeded fresh from `tasks.json` for every run.

The task set has 8 tasks. Each one needs two or three independent lookups
(`read`, `grep`, `bash`) over a small seeded project (`src/`, `data/*.csv`,
`logs/server.log`, `docs/notes.md`, `README.md`). A checker
(`contains_all`: whole-token, case-insensitive) is applied to the settle
answer.

Per run it reports: pass/fail (checker + `settle{done}` + the log replays
clean), turns, prompt/completion tokens (summed `llm_response.usage`), tool
calls, `par` (the most calls in one response), `ph` (placeholders), and wall
time (task sent → `settle` delivered). Then it reports totals per mode. With
`--json` / `--out` the full rows go to JSON, including each answer and the
values the checker missed.

### CI

`--fake` scripts the LLM from each task's `fake` entry: one response with the
parallel calls, then the answer. The tools still really run in the guest. In
fake mode a run passes only if the tool results themselves contain the
checker's values and no tool returned an error, which checks that the seed,
the tools and the task agree. `tests/test_bench_async_tools.py` runs it (and
the `--llm-url` path against a local OpenAI-compatible double) in
`uv run pytest`.

### On the server (vLLM, Qwen3-14B)

```
cd harness
uv run python -m bench.async_tools \
    --llm-url http://127.0.0.1:8000 --model Qwen/Qwen3-14B \
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
  guest's per-task limit. `--work-dir DIR --keep` keeps every run's directory
  (the guest log under `log/events/`, the workspace under `ws/`), so a run can
  be inspected with `python -m protocol.replay replay DIR/<run>/log`.
- The exit code is 0 only when every run passed.

Run it with nothing else using vLLM, so the wall times are comparable.
Record the date, the vLLM version and the model revision.

## Results

To be filled in on the server once async mode is implemented in the
coordinator (`ph` > 0 in the `on` rows). Use totals over `--runs 3`.

| Date | vLLM / model | Mode | Passed | Turns | Prompt tok | Completion tok | Tool calls | Placeholders | Wall s |
|---|---|---|---|---|---|---|---|---|---|
| | Qwen/Qwen3-14B | off | /24 | | | | | 0 | |
| | Qwen/Qwen3-14B | on | /24 | | | | | | |

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

**Finding:** _pending._ Until it is recorded, `async_tools = False` stays the
default (D6).
