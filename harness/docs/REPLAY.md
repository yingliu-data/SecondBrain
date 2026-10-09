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
still open. The prefix only ever grows, except at a compaction.

## The ten invariants

For each: what `fold.py` checks now (M0) and what is deferred. M2 notes are marked inline.

| # | Invariant | M0 (fold.py) | Deferred |
|---|---|---|---|
| 1 | **What turn T saw is `events[seq <= T.includes_through_seq]`.** The request the model received is a function of the log alone. | `turn_inputs(events, turn_id)` returns the committed prefix as of the turn's `turn_start`; `turn_start` commits only items with `seq <= includes_through_seq`; `includes_through_seq` must be `< seq` and never go backwards. | M1: rebuild the full request (system block + prefix + preamble) with the guest context builder and compare its hash with `turn_start.request_hash`; `preamble_hash` likewise. |
| 2 | **The committed prefix is never rewritten.** Corrections are appended; the system block is fixed per session. | A `tool_result` for a call whose placeholder was committed appends a NEW item (`counters.placeholder_fills`); the prefix list is only appended to by `turn_start` and rebuilt only by `compaction`; `prefix_origins` makes the provenance of each item explicit; tests compare the canonical JSON of the prefix before and after late results. | M1: assert in the guest that the serialized request bytes for the prefix part are byte-identical between consecutive turns (prefix-cache check). |
| 3 | **`state = fold(events)`; replay never calls out.** | `fold` is pure (purity test patches `time`, `os.urandom`, `random`, `os.environ`); no I/O in `protocol/` (`test_purity.py`). | M6: run the replay verifier on every CI log after the run. |
| 4 | **Inputs are idempotent by `input_id`.** | `input{dedupe: duplicate\|late}` changes only `seen_input_ids` and a counter; a `dedupe: new` input whose `input_id` was seen is a `FoldError`. | M5: the Temporal session workflow dedupes task signals; continue-as-new carries the seen ids. |
| 5 | **Result arrival order does not change the prefix.** Two results in swapped order fold to the same committed prefix. | Tool items are ordered by the seq of their `tool_call`; `test_invariant5_swapped_result_order_folds_to_same_prefix`. | None for sync mode. M6 (async option): placeholders plus late fills are already modelled; measure with Qwen3. |
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
