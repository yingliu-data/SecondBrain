# Deterministic replay (`protocol/fold.py`)

`state = fold(events)`. The fold is pure: it never reads the clock, the
environment, randomness or a file, and it never calls the LLM, a tool or the
gateway. `ts` is data; the fold never reads it (time inside the fold comes from
`clock` events only). A replay therefore needs the log and nothing else: the
same events, in any environment, at any later date, give the same `State` and
the same `state_hash`.

```
replay(events) -> (State, errors)      validate_log + verify_checkpoints + fold, folding once
fold(events, upto_seq=None, *, state=None) -> State
                                       raises FoldError("seq N: ...") on an impossibility;
                                       with `state`, continues a kept State (events with
                                       seq <= state.last_seq are skipped): the host's
                                       per-turn path
apply(state, event) -> State           one step of the fold, in place
state_hash(state) -> str               hash_value(state.to_canonical())
verify_checkpoints(events, *, last_only=False) -> [str]
                                       checkpoint hashes and the prev_hash chain; with
                                       last_only, only the final checkpoint's hash is
                                       recomputed (the chain is still checked for all)
turn_inputs(events, turn_id) -> [msg]  what turn T saw: the prefix as of includes_through_seq
```

`apply` runs `events.validate_event` on every event first, so the fold never re-checks
shapes or enums: a malformed event is a `FoldError` carrying its first schema error. The
handlers do state transitions and the reference rules that need the folded state
(docs/EVENTS.md, "Rules the fold checks").

## What the fold keeps

| Field | Meaning |
|---|---|
| `user`, `tenant`, `session_id`, `config_hash`, `system_hash`, `tools` | fixed by `session_start`; `tools` is the allowlist (name, side, pin) |
| `committed` | the items (`{seq, origin, message}`) the model has seen, in commit order. Hashed as `committed_prefix` (the messages) and `prefix_origins` (the origin seqs); `State.committed_prefix` / `State.prefix_origins` are read-only views |
| `staged_suffix` | items (`{seq, origin, message}`) appended to the log but not yet seen by a turn, sorted by `(origin, seq)` |
| `open_calls` | `call_id -> {name, side, turn_id, state, seq, result?, duration_ms?, spill_ref?}`; a closed call keeps its entry with `result` = the seq of its `tool_result` |
| `placeholders` | call_ids the model saw as `[pending]` tool messages (async mode) |
| `queues.follow_up`, `queues.steer` | the seqs of staged `follow_up` / `steer` inputs not yet committed (the messages themselves are in `staged_suffix`) |
| `open_questions`, `denied_questions` | `question_id -> {deadline, call_id, answered, seq, answer?}`; count of `guard_decision{deny, reason: unanswered}` |
| `session_memory` | `slug -> sha256` for `memory{scope: session}` |
| `usage`, `limits_consumed` | summed `llm_response.usage` (prompt/completion tokens); `turns` and `ops` |
| `last_settle`, `parked_at` | the latest `settle`; the checkpoint seq of the park (`None` when not parked). `parked` is derived (`parked_at is not None`) and still emitted in the hash |
| `turns` | `turn_id -> {turn_id, seq, includes_through_seq, type, model, request_hash, preamble_hash, workspace_tree_id, response_seq, finish_reason?}`, insertion ordered; hashed as the list of values. `State.last_turn()` is the latest |
| `seen_input_ids`, `clock_at`, `compacted_through`, `counters`, `last_seq` | bookkeeping |

`to_canonical()` is `dataclasses.asdict` with sets as sorted lists, the private index
fields (`_unresolved`: calls without a result; `_checkpoint_seqs`) dropped, and the
`committed` / `parked` / `turns` renderings above. The emitted keys are the hash contract;
the three goldens pin it. Logs with `follow_up` or `steer` inputs hash differently from
the first draft of the fold (queues held `{seq, input_id}` dicts and steers were not
staged); the goldens have none, so their checkpoints are unchanged.

**Message shapes.** A user item is `{role: "user", content, input_id, kind, preamble}`; an
assistant item is the `llm_response.message` dict verbatim; a tool item is
`{role: "tool", tool_call_id, content}` where `content` is `rewrite` when present, else
`content`. The `preamble` marker on the first user message a turn commits is that
turn's `preamble_hash` (the per-turn preamble text itself, time and recalled memories,
is rebuilt by the guest context builder in M1); other user messages carry `None`.

**Ordering.** Every item has an `origin` seq: an input's own seq, an `llm_response`'s own
seq, and, for a tool message, the seq of its `tool_call`. The staged suffix stays sorted by
origin, so tool results fold in call order whatever order they arrived in. A `turn_start`
commits every staged item whose seq is `<= includes_through_seq` (tasks, follow-ups,
steers, responses and results alike), plus a `[pending]` placeholder for each call that was
still open. The prefix only ever grows, except at a compaction. The guest's context builder emits the
same placeholders (`guest.context.placeholder_items`, same rule, same order) when it builds
a turn's request, so the `request_hash` of an async turn is rebuilt from the fold of the
log just before its `turn_start` (`harness/tests/test_guest_async.py::rebuilt_request_hashes`).
A late fill renders as a user message `[late tool result] call_id: <id>, tool: <name>`
followed by the content (docs/GUEST.md "Async mode").

## The ten invariants

For each: what `fold.py` checks now (M0) and what is deferred. M2 notes are marked inline.

| # | Invariant | M0 (fold.py) | Deferred |
|---|---|---|---|
| 1 | **What turn T saw is `events[seq <= T.includes_through_seq]`.** The request the model received is a function of the log alone. | `turn_inputs(events, turn_id)` returns the committed prefix as of the turn's `turn_start`; `turn_start` commits only items with `seq <= includes_through_seq`; `includes_through_seq` must be `< seq` and never go backwards. | M1: rebuild the full request (system block + prefix + preamble) with the guest context builder and compare its hash with `turn_start.request_hash`; `preamble_hash` likewise. |
| 2 | **The committed prefix is never rewritten.** Corrections are appended; the system block is fixed per session. | A `tool_result` for a call whose placeholder was committed appends a NEW item (`counters.placeholder_fills`); the prefix list is only appended to by `turn_start` and rebuilt only by `compaction`; `prefix_origins` makes the provenance of each item explicit; tests compare the canonical JSON of the prefix before and after late results. | M1: assert in the guest that the serialized request bytes for the prefix part are byte-identical between consecutive turns (prefix-cache check). |
| 3 | **`state = fold(events)`; replay never calls out.** | `fold` is pure (purity test patches `time`, `os.urandom`, `random`, `os.environ`); no I/O in `protocol/` (`test_purity.py`). | Done (M6): `tests/conftest.py` runs `protocol.replay` over every log a test wrote (see "CI logs" below). |
| 4 | **Inputs are idempotent by `input_id`.** | `input{dedupe: duplicate\|late}` changes only `seen_input_ids` and a counter; a `dedupe: new` input whose `input_id` was seen is a `FoldError`. | M5: the Temporal session workflow dedupes task signals; continue-as-new carries the seen ids. |
| 5 | **Result arrival order does not change the prefix.** Two results in swapped order fold to the same committed prefix. | Tool items are ordered by the seq of their `tool_call`; `test_invariant5_swapped_result_order_folds_to_same_prefix`. | None for sync mode. Async mode (`async_tools`): built in the guest (docs/GUEST.md "Async mode"), placeholders and late fills tested in `harness/tests/test_guest_async.py`; measured with Qwen3 per docs/ASYNC-TOOLS.md. |
| 6 | **`ts` is data, never a fold input.** | The fold never reads `ts`; `test_fold_never_reads_ts` sets every `ts` to `"x"` and gets the same `state_hash`. `clock{at}` is the only time. | M1: the guest's idle/grace/question timers must be recorded as `clock` events before they act. |
| 7 | **Checkpoints chain and match the fold.** `checkpoint{through_seq T}` carries `state_hash(fold(events[..T]))`; `prev_hash` is `hash_value(session_start)` for the first, then the previous checkpoint's `state_hash`. | `verify_checkpoints` recomputes every hash in one pass and checks the chain and monotonic `through_seq` (`last_only=True` recomputes only the final hash); `validate_log` only checks `through_seq < seq`. M2: the guest emits no checkpoint yet, but every coordinator scenario (`tests/test_guest_coordinator.py::check_log`) and the e2e runs (`tests/test_guest_e2e.py`) assert `state_hash(replay(log))` equals the hash of the `State` the coordinator folded live, through compaction turns, overflow retries, budget failures and memory events. | M3/M5: park/resume restores from a checkpoint, so the restored guest must re-derive the same hash before continuing (resume test). |
| 8 | **A tool could only have been called if it was offered.** `tool_call.{name, side}` must be in `session_start.tools`; pins are part of `session_start`. | `FoldError` on a call not offered (name or side mismatch, via `allowlist.is_allowed`); `tools` with pins is in `State` and in the hash, so a replay against a changed upstream still folds. | M4: the gateway quarantines a changed definition (pin mismatch) and the guest records an error result; `validate_event` already rejects the schema case. |
| 9 | **Derived ids are deterministic.** `turn_id`, `question_id`, operation ids are `derive_id(user, tenant, session_id, seq, kind)`; `call_id` is vLLM's, verbatim. | Goldens mint ids with `derive_id`; the fold treats ids as opaque and checks references (unknown turn, call, question → `FoldError`). | M1: the guest mints ids the same way; add a check that `turn_id == derive_id(..., turn_start.seq, "turn")` once the guest exists. |
| 10 | **Compaction is a turn plus an event; raw events stay.** The prefix after `compaction{range, summary_turn_id}` is `items before range + summary assistant message + items after range`. | Implemented exactly so; the summary's `llm_response` is the compaction turn's response and takes the position of the range; items inside the range are dropped from prefix and suffix; `compacted_through` is in the hash. | M2 (done in the guest): a compaction turn runs before a regular turn whose estimate (`guest.context.estimate_tokens`, ceil(`Request.chars`/4), × a usage correction) exceeds `context_window_tokens − reserve_tokens`, and once on a provider overflow (the regular turn is retried; a second overflow fails the task). The compaction turn's `includes_through_seq` is the previous turn's (it commits nothing) and `range` is `[first prefix origin, includes_through_seq]`, so the summary replaces exactly the committed prefix and whatever was staged follows it. `tokens_before/after` are still reported, not checked (`tokens_after` comes from a copy of the state with the same `compaction` event applied, so it uses the fold's rebuild rule). The 40-turn e2e (`test_forty_turn_session_compacts_and_replays`) checks the prefix stays bounded and the log replays. |

Plus the section 12 additions: `tool_call.side` and `session_start.tools` are in `State`
(invariant 8); host-originated results replay from `tool_result` like any other (the fold
does not care which side ran the operation); the gateway pins are in `session_start`.

## Rules the fold applies, per event type

- `session_start`: must be seq 1 (so also unique); fixes session_id, user, tenant, hashes, tools.
- `input`: dedupe first, whatever the kind: `duplicate`/`late` only touch `seen_input_ids`
  and a counter; `new` with a seen `input_id` → `FoldError`. Then `task`/`follow_up`/`steer`
  stage a user message (`kind` carried; `follow_up` and `steer` seqs also queued until
  committed); `ui_response` marks `open_questions[question_id]` answered with
  `body.answer` (unknown or twice-answered → `FoldError`) and stages nothing.
- `clock`: `clock_at = at`.
- `turn_start`: `includes_through_seq` below its seq and not behind the previous turn,
  `turn_id` unique; commits (see Ordering), records the turn, `limits_consumed.turns += 1`.
- `llm_response`: turn must exist; stages the message verbatim, sums usage; second response
  for a turn → error.
- `tool_call`: turn must exist, `call_id` unique, `(name, side)` offered
  (`allowlist.is_allowed`); opens the call, `limits_consumed.ops += 1`.
- `guard_decision`: counted; `deny` with `reason == events.UNANSWERED` → `denied_questions += 1`.
- `op_state`: updates `state` (and `duration_ms`, `attempt`); `input_required` (mcp
  calls only, else error) keeps `input_request` on the call, any other state drops it;
  unknown call → error.
- `tool_result`: unknown call → error. One late rule: the result is late iff the call
  already has a result or its placeholder was committed, and `body.late` must say so
  (either way round is a `FoldError`). A second result is recorded in
  `counters.late_results` and ignored; a first result after a committed placeholder is a
  new item with origin = its own seq (`counters.placeholder_fills`); any other first result
  closes the call and takes the call's position (origin = the `tool_call` seq).
- `question`: opens the question (unknown call, duplicate `question_id` → error).
  `parked`: `checkpoint_seq` must be an earlier checkpoint's seq; sets `parked_at`.
- `compaction`: summary turn must exist, be of type `compaction`, have its response, and
  `range[1]` be below it; rebuilds the prefix from the summary (invariant 10).
- `memory`: `scope: session` updates `session_memory`; `scope: user` is counted only.
- `bulk`, `error`, `wake`, `checkpoint`: counters. `settle`: `last_settle`, clears `parked_at`.
- Any event before `session_start`, a seq gap, a wrong `session_id`, a schema error → `FoldError`.

## How to read a replay error

`replay(events)` returns `(state, errors)`. Every error is `"seq N: message"`, the list is
sorted by seq and de-duplicated, so **the first entry names the first seq at which the log
is wrong**. `state` is the fold up to the first `FoldError` (or the full fold when the fold
itself succeeded and only the checks failed). `replay` folds the log once: the same pass
produces the state, the hashes at every checkpoint's `through_seq`, and the first
`FoldError`.

| Error | Means | Look at |
|---|---|---|
| `seq N: body.x: ...`, `seq N: ... names no input` | schema or id rule from `validate_log` (a schema error also stops the fold at N) | the event at seq N |
| `seq N: expected seq M (gapless from 1)` | a missing or reordered event | the writer / the state sync before seq N |
| `seq N: tool 'x' side 'y' not offered in session_start.tools` | a call the session could not have made | the allowlist at seq 1, the guard |
| `seq S: checkpoint state_hash mismatch: fold through seq T gives A..., log says B...; the state diverged in seqs P..T` | the log and the fold disagree somewhere in `P..T` (P = the previous verified checkpoint + 1). Either an event in that window was altered after the checkpoint was written, or the fold rules changed between writer and reader. | the events in `P..T`; diff the writer's and the reader's `protocol/fold.py` version |
| `seq S: checkpoint.prev_hash ... does not continue the chain` | checkpoints were reordered, dropped or the session_start changed | the previous checkpoint and seq 1 |
| `seq N: <FoldError text>` | a structural impossibility (result for an unknown call, turn before session_start, second response for a turn, ...) | the event at seq N; `state.last_seq == N - 1` |

To localise a mismatch inside a window, fold with `upto_seq` and compare `state_hash` (or
`to_canonical()`) against a known-good run, bisecting on the seq. A checkpoint per settle is
the M0 granularity; M6 may add intermediate checkpoints.

## Goldens

`tests/golden/make_goldens.py` writes the three frozen logs deterministically (fixed `ts`
strings, `derive_id` ids, fixed `call_id`s, checkpoint hashes computed by the fold):

- `text_answer.jsonl`: session_start, input, clock, turn_start, llm_response (no tools), settle, checkpoint.
- `device_tool_late_duplicate.jsonl`: a device `calendar` call with guard, op_state, tool_result, wake, a second turn, settle, then a duplicate input and a late tool_result for the terminal call, checkpoint.
- `mcp_tool_via_gateway.jsonl`: an mcp-side tool with a pin in `session_start.tools`, guard allow, op_state, a `bulk{spill}` and a `tool_result` with `spill_ref`, a second turn, settle, checkpoint.

The generator builds on `tests/logbuilder.py` (the same `Log` the tests use) and
`allowlist.effective_tools` for the tool list. `test_golden_generator_is_deterministic`
renders each golden in memory and requires byte equality with the frozen file, so a fold
rule change that moves a hash must be accompanied by re-running the generator
(`python3 tests/golden/make_goldens.py`) and reviewing the diff.

## Replay / verify CLI (M6, `protocol/replay.py`)

```
cd harness
python -m protocol.replay replay <target>...          # fold; per run: seqs, turns, calls, settle, state_hash
python -m protocol.replay verify <target>...          # validate_log + fold + checkpoint chain, per run
python -m protocol.replay verify --all <root>...      # every session dir under each root
                                                       # (--json on either)
```

A target is a session directory (`<session>/events/rNNNN/NNNNNN.jsonl`, runs in
order, segments in order; the host log `events/host-NNNNNN.jsonl`), its
`events/` directory, a flat log directory (`<log_dir>/events/NNNNNN.jsonl`, the
guest's local buffer: one run named `events`) or a single `.jsonl` file. The
layout is read from the files alone; nothing from agent-api is imported.
`--all` uses `find_sessions(root)`: every directory under the root with an
`events/` directory holding runs, segments or a host log (legacy sessions with
only `history.jsonl` are skipped).

`verify` exits 0 when every target is clean, else 1, printing one line per
problem: `<target>: <run>: seq <n>: <reason>` (run = `r0001`, ..., `events` for
a flat log, or the file name for a `.jsonl` target) and
`<target>: host: hseq <n>: <reason>` for the host log. Checks, per run: every
line is a JSON object (an unreadable or torn line names the seq it would have
had), `fold.replay` (validate_log, the fold, every checkpoint's hash and the
prev_hash chain); across runs: one `session_id`; the host log: `hseq` gapless
from 1, `type` str, `body` dict. `replay` prints the fold summary
(`summarize(state)`) per run and the same problems; it exits 1 on any.

Public functions (agent-api reuses them): `load_session(path) -> list[list[event]]`
(the runs; `ReplayError` when a file is unreadable), `verify_session(path) ->
list[str]` (`"<run>: seq N: reason"`, `[]` == clean), `find_sessions(root)`,
`summarize(state)`.

## CI logs (M6)

`tests/conftest.py` has an autouse fixture that runs after every test, after
the test's own fixtures have stopped the guests:

- the events each `FakeHost` received are split into runs at each
  `session_start` and replayed with `fold.replay`;
- every log directory under the test's `tmp_path` (`find_sessions`: guest log
  dirs, session dirs) and every `events.before-resume-N` buffer a log return
  moved aside goes through `verify_session`.

Any problem fails the test (a teardown error that names the log and the seq).
Tests that write synthetic logs on purpose (the event buffer and transport
unit tests, the tampering tests) opt out with `@pytest.mark.no_log_verify`.
When it was added it checked about 34 fake-host logs and 39 log directories per
run of the suite (the e2e, M3, M4 and M5 guest subprocess tests); none failed.

## PLAN.md section 14 → tests

Paths are relative to the repo root. The owner is the suite whose behaviour the
bullet is. **missing** means no test covers the bullet (or that part of it) yet.

| # | Edge case (PLAN 14) | Test(s) | Owner / status |
|---|---|---|---|
| 1 | Same `input_id` twice → one task, one turn; second POST attaches | `harness/tests/test_guest_coordinator.py::test_redelivered_task_is_recorded_duplicate_and_runs_once`, `harness/tests/test_guest_e2e.py::test_redelivered_task_runs_once`, `harness/tests/test_fold.py::test_duplicate_input_folds_once`, `harness/tests/test_guest_inbox.py::test_duplicate_by_input_id`; `agent-api/tests/test_host_worker.py::test_redelivered_while_running_attaches_and_guest_dedupes`, `agent-api/tests/test_host_e2e.py::test_e2e_redelivered_input_runs_once`, `agent-api/tests/test_host_m3.py::test_redelivered_input_after_park_resume_runs_once`; `flows/flows/tests/test_session_workflow.py::test_a_duplicate_task_signal_runs_once` | guest + agent-api + flows; covered |
| 2 | `finish_reason = length` → no tool runs | `harness/tests/test_guest_coordinator.py::test_finish_reason_length_runs_no_tools` | guest; covered |
| 3 | Malformed `arguments` → one repair turn, then an error result | `harness/tests/test_guest_coordinator.py::test_malformed_arguments_get_an_error_result_without_running`, `::test_malformed_arguments_error_result_then_the_next_turn_repairs_the_call` (M6) | guest; covered as the guest behaves today: the error result comes first and the next turn is the repair turn (coordinator docstring, "M1 deviations"). A repair turn *before* any error result is not implemented |
| 4 | Tool not offered or side mismatch → `guard_decision{deny, allowlist}`; in a log a schema error | `harness/tests/test_guest_coordinator.py::test_allowlist_denial_records_a_decision_only_and_fills_the_gap`, `::test_side_mismatch_is_an_allowlist_denial`, `harness/tests/test_fold.py::test_tool_call_not_offered_is_rejected`, `harness/tests/test_allowlist.py::test_is_allowed_matches_name_and_side` | guest + protocol; covered |
| 5 | Device result for unknown/terminal `call_id` → 409, `input{dedupe: late}` | `agent-api/tests/test_host_routes.py::test_harness_chat_streams_and_device_result_resolves`, `agent-api/tests/test_host_sides.py::test_device_call_resolves_through_registry_and_409_semantics`, `agent-api/tests/test_host_m3.py::test_death_mid_call_resumes_from_base_with_log_return_and_lost_call`; guest side `harness/tests/test_guest_coordinator.py::test_side_result_for_unknown_or_terminal_call_is_logged_and_ignored`, `harness/tests/test_fold.py::test_late_result_for_terminal_call_is_recorded_and_ignored` | owned by agent-api; covered |
| 6 | Device result after an agent-api restart → accepted, task completes | `agent-api/tests/test_host_edge_cases.py::test_device_result_after_an_agent_api_restart_is_accepted_and_the_task_completes` (`[kept]`: the frozen guest outlives the old process, session-snapshot restore; `[killed]`: base restore + log return ending with `parked`), `::test_a_restart_after_a_guest_death_does_not_adopt_device_calls`, `agent-api/tests/test_host_sides.py::test_adopted_device_call_delivers_once_and_keeps_the_409_semantics` | owned by agent-api; covered for a **parked** session: a new Worker rebuilds the open device calls from the fold (`Worker._adopt_open_device_calls`, `DeviceCalls.adopt`); POST /tool-result for an id the process does not know loads the tenant's parked sessions first (`Workers.load_parked`), so the result is accepted and wakes the guest. A guest that was running (not parked) when agent-api went away is gone with it; its resume marks the call `lost`, so that result stays a 409 (agent-api's shutdown does not park running sessions) |
| 7 | Client disconnects mid-task → task continues; `GET /events?after_seq` catches up | `agent-api/tests/test_host_temporal.py::test_client_disconnect_mid_task_direct_path`, `::test_client_disconnect_mid_task_workflow_path` (the SSE client sends `http.disconnect` after the `tool_call`; the device answers, the task settles, `after_seq` returns the rest, the same input_id replays the answer), `::test_chat_task_completes_through_the_workflow`, `agent-api/tests/test_host_m3.py::test_bulk_get_returns_the_acked_log_after_seq` | owned by agent-api; covered |
| 8 | MCP tool exceeds `timeout_s` → `op_state{timeout}`, error result, turn continues | `harness/tests/test_guest_m4.py::test_op_timeout_from_the_hello_entry_closes_the_call`, `::test_mcp_op_that_expires_while_parked_is_a_timeout_without_reattach`; `mcp-gateway/tests/test_tool_server.py::test_upstream_timeout` | guest + gateway; covered |
| 9 | Upstream definition changes under a pinned session → quarantine; new session sees the new pin | `mcp-gateway/tests/test_tool_server.py::test_quarantine_mid_session`, `::test_new_session_pins_fresh_definitions_after_ttl`, `::test_pins_persist_across_restart` | owned by mcp-gateway; covered |
| 10 | Gateway unreachable → error result; task continues; `gateway{error}` host event | `harness/tests/test_guest_m4.py::test_gateway_unreachable_is_an_error_result`; `agent-api/tests/test_host_gateway_forwarder.py::test_unreachable_gateway_is_502_and_a_host_event` | guest + agent-api; covered |
| 11 | vLLM 502/503/504 or down → retries, auto-start path, one `error` after the last retry | `harness/tests/test_guest_provider.py::test_retries_502_then_succeeds_with_backoff`, `::test_gives_up_after_retries_with_a_transient_error`, `::test_connection_error_is_transient`, `harness/tests/test_guest_coordinator.py::test_provider_error_settles_failed`; `agent-api/tests/test_host_llm_proxy.py::test_upstream_error_status_is_passed_verbatim`, `::test_unknown_path_and_unreachable_upstream`, `agent-api/tests/test_host_edge_cases.py::test_vllm_down_retries_in_the_guest_then_one_error` (`[unreachable]`, `[503]`: real guest behind the proxy, 3 attempts each a `usage` host event, one `error`, settle `failed`, one SSE error) | guest + agent-api; covered. The retries live in the guest provider; the proxy passes every status through (502 `upstream_unreachable` when nothing listens) and does not retry. There is no auto-start path in agent-api (vLLM is started outside it), so that part of the bullet has nothing to test |
| 12 | Result over `spill_threshold` → truncated event, full text on :5001, `bulk` with sha256 | `harness/tests/test_guest_ops.py::test_spill_above_threshold_with_sha256`, `harness/tests/test_guest_coordinator.py::test_spilled_result_emits_bulk_and_spill_ref`, `harness/tests/test_guest_m3.py::test_settle_manifest_and_spill_upload`; `agent-api/tests/test_host_m3.py::test_bulk_put_spill_workspace_and_manifest` | guest + agent-api; covered |
| 13 | Context over window − reserve → compaction turn, retry once, then `failed` | `harness/tests/test_guest_coordinator.py::test_window_triggers_a_compaction_turn_before_the_regular_turn`, `::test_overflow_compacts_and_retries_the_turn_once`, `::test_second_overflow_after_compaction_fails_the_task` | guest; covered |
| 14 | `max_tools` reached → wrap-up turn without tools, `settle{done}` | `harness/tests/test_guest_coordinator.py::test_max_tools_wrap_up_turn_has_no_tools_then_settles_done`, `::test_calls_beyond_max_tools_in_one_response_are_denied_with_limit` | guest; covered |
| 15 | Question deadline, `ui_attached=false` → park; answer → resume; `answer_wait_s` → `escalated` | `harness/tests/test_guest_m5.py::test_deadline_parks_with_question_id_then_answer_after_resume_completes`, `::test_abort_escalated_after_resume_settles_escalated`, `::test_e2e_question_deadline_parks_then_answer_after_reconnect`; `agent-api/tests/test_host_temporal.py::test_question_parks_then_answer_resumes`, `::test_answer_wait_timer_escalates`; `flows/flows/tests/test_session_workflow.py::test_b_parked_then_answer_resumes`, `::test_c_parked_timer_expiry_escalates` | guest + agent-api + flows; covered |
| 16 | Doom loop → question | `harness/tests/test_guest_coordinator.py::test_doom_loop_asks_and_stop_fails_the_call`, `::test_doom_loop_continue_runs_the_call` | guest; covered |
| 17 | Park mid-bash waits for the turn boundary; bash past `park_grace_s` killed, `op_state{killed}` | `harness/tests/test_guest_m3.py::test_park_mid_bash_waits_the_grace_then_kills`, `harness/tests/test_guest_m3_units.py::test_park_grace_kills_guest_ops_and_records_killed`, `::test_park_waits_for_the_provider_then_parks_at_the_turn_boundary` | guest; covered |
| 18 | Two VMs from one base snapshot → distinct ids and logs | `harness/tests/test_guest_m3_units.py::test_same_pre_hello_state_different_session_ids_give_distinct_ids_and_logs`, `::test_no_randomness_or_uuid_in_the_control_path`; `agent-api/tests/test_sandbox_snapshot.py::test_two_sessions_restore_from_one_base` | guest + agent-api; covered (the real-KVM restore ×2 is the server job, not pytest) |
| 19 | VM dies mid-tool → log-return resume, open call `lost`, task completes | `harness/tests/test_guest_m3.py::test_kill_mid_bash_resume_with_log_return`, `harness/tests/test_guest_m3_units.py::test_restore_marks_open_calls_lost_and_continues`; `agent-api/tests/test_host_m3.py::test_death_mid_call_resumes_from_base_with_log_return_and_lost_call` | guest + agent-api; covered |
| 20 | Idle past `session_idle_s` → snapshot + destroy; next task restores | `agent-api/tests/test_host_m3.py::test_idle_timer_parks_snapshots_destroys_and_the_next_task_restores`; guest side `harness/tests/test_guest_coordinator.py::test_park_between_tasks_parks_at_once_and_the_link_resumes_it` | owned by agent-api; covered |
| 21 | Heartbeat stops → `run_task` retried once from the last heartbeat `seq`; a second failure fails the task, the workflow stays signalable | `flows/flows/tests/test_session_workflow.py::test_d_run_task_retry_receives_heartbeat_details`, `::test_d2_run_task_failing_twice_fails_the_task_and_the_workflow_stays_signalable`, `agent-api/tests/test_host_temporal.py::test_run_task_retry_reattaches_without_a_second_turn` | owned by flows; covered |
| 22 | History past 10 000 events or 50 tasks → continue-as-new carries seen `input_id`s and parked state | `flows/flows/tests/test_session_workflow.py::test_e_continue_as_new_after_n_tasks`, `::test_e2_continue_as_new_on_history_length`, `::test_b3_carried_parked_task_resumes_in_new_run`, `::test_b6_history_hop_while_parked_then_answer` | owned by flows; covered (with small thresholds) |
| 23 | Replay mismatch or broken checkpoint chain → hard error naming the seq | `harness/tests/test_fold.py::test_corrupted_event_reports_first_diverging_seq`, `::test_broken_prev_hash_is_an_error`; `harness/tests/test_replay.py::test_altered_event_fails_at_the_checkpoint_naming_its_seq`, `::test_bad_checkpoint_hash_names_the_checkpoint_seq`, `::test_gap_names_the_seq_after_it`, `::test_broken_prev_hash_chain_names_the_seq`, `::test_cli_verify_all_reports_failing_sessions_with_seq` (M6) | protocol; covered |
| 24 | A hook reading `time()` → purity test fails | `harness/tests/test_guest_hooks.py::test_impure_before_hook_fails_the_purity_check`, `::test_impure_after_hook_fails_the_purity_check`; `harness/tests/test_fold.py::test_fold_is_pure` | guest + protocol; covered |
