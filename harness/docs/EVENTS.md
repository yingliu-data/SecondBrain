# Event schema (schema version 1)

Source of truth: `protocol/events.py` (`REQUIRED_BODY`, `OPTIONAL_BODY`, `ENUMS`,
`MIN_INT`, `STR_LISTS`, `OPEN_ENUMS`, `validate_event`, `validate_log`) and, for the
`session_start.tools` entries, `protocol/allowlist.py` (`tools_errors`). This page
documents what those modules enforce.
PLAN.md section 5 is the design; this is the contract.

## Envelope

Every event is a JSON object with exactly these keys:

| key | type | rule |
|---|---|---|
| `session_id` | str | non-empty; one value per log |
| `seq` | int | `>= 1`; gapless and strictly increasing from 1 within a log |
| `ts` | str | ISO-8601 wall-clock time. Data only, never a fold input |
| `schema` | int | must equal `SCHEMA_VERSION` (1); any other value is an error |
| `type` | str | one of the 18 types below |
| `body` | dict | per-type keys below; extra body keys are allowed; unknown envelope keys are not |

Bodies must be canonicalisable (`protocol/canonical.py`): string keys and no NaN or infinity (finite floats are fine),
because checkpoints and derived ids hash events. A NaN or infinity anywhere in a body is a
schema error; finite floats canonicalise by their shortest round-trip text.

`make_event(session_id, seq, ts, type, body)` builds an envelope without validating it.

## Types and bodies

Types marked `?` are optional. `int` never accepts `bool`. Hashes are lowercase hex sha256
strings. Times (`at`, `deadline`) are int milliseconds since the epoch.

| type | body | writer |
|---|---|---|
| `session_start` | `user` str, `tenant` str, `workflow_run_id` str, `config_hash` str, `system_hash` str, `tools` list of `{name: str, side: guest/host/device/mcp, pin: str or null}` (unique names, `pin` key required), `harness_version` str, `image_version` str, `resumed` bool | guest main |
| `input` | `input_id` str, `kind` in `task`/`follow_up`/`steer`/`ui_response`, `text` str, `dedupe` in `new`/`duplicate`/`late`, `question_id`? str, `answer`? str | inbox |
| `clock` | `kind` in `host_hello`/`host_heartbeat`/`turn_clock`/`idle_timer`/`question_deadline`/`wall_clock` or any `grace_*`, `at` int ms | coordinator |
| `turn_start` | `turn_id` str, `type` in `regular`/`compaction`, `includes_through_seq` int `>= 0`, `model` str, `params` dict, `request_hash` str, `preamble_hash` str, `workspace_tree_id` str or null | coordinator |
| `llm_response` | `turn_id` str, `message` dict (the assistant message verbatim), `finish_reason` str, `usage` dict, `response_id`? str or null | provider |
| `tool_call` | `turn_id` str, `call_id` str (vLLM's, verbatim), `name` str, `side` in `guest`/`host`/`device`/`mcp`, `args` dict, `state` == `"RUNNING"` | submit |
| `guard_decision` | `ref` str (a call_id or question_id), `decision` in `allow`/`deny`/`ask`, `reason` str (free text, not an enum: `allowlist`, `unanswered`, `limit`, `placeholder_pass`, `mode`; the fold counts `events.UNANSWERED` = `"unanswered"`), `policy_id` str | guard / coordinator |
| `op_state` | `call_id` str, `state` in `queued`/`running`/`done`/`failed`/`timeout`/`lost`/`killed`, `duration_ms`? int `>= 0`, `pid`? int, `side_ref`? str | ops / resume |
| `tool_result` | `call_id` str, `content` str (verbatim, truncated at `spill_threshold`), `spill_ref`? str or null, `rewrite`? str or null (after_tool output), `late`? bool | ops / hooks |
| `wake` | `reason` in `input`/`tool_result`/`grace`/`idle`/`question`, `result_ids` list of str, `input_ids` list of str | coordinator |
| `question` | `question_id` str, `call_id` str, `text` str, `options` list of str, `deadline` int ms `>= 0` | tools |
| `parked` | `checkpoint_seq` int `>= 1`, `reason` str | coordinator |
| `compaction` | `range` `[from_seq, to_seq]` two ints with `1 <= from <= to`, `summary_turn_id` str, `tokens_before` int `>= 0`, `tokens_after` int `>= 0` | coordinator |
| `memory` | `scope` in `user`/`session`, `op` in `write`/`remove`, `slug` str, `sha256` str, `derived_from` str (a call_id or a compaction `summary_turn_id`) | host |
| `bulk` | `kind` in `spill`/`workspace`, `path` str, `sha256` str, `bytes` int `>= 0` | ops |
| `checkpoint` | `through_seq` int `>= 1`, `state_hash` str, `prev_hash` str | coordinator |
| `error` | `class` in `transient`/`overflow`/`budget`/`limit`/`protocol`/`fatal`, `message` str, `retry` bool (whether the harness will retry), `ref`? str | any |
| `settle` | `input_id` str (the task's), `outcome` in `done`/`failed`/`timeout`/`escalated`, `answer` str, `usage` dict (totals), `manifest` dict or list (workspace manifest) | coordinator |

## M3 notes (what the guest writes)

- `clock.kind` `grace_park`: a park's grace expired with guest operations
  running; they are killed next (`op_state{killed}` + an error `tool_result`).
- `checkpoint` + `parked` are written back to back when the guest parks:
  `checkpoint.through_seq` is the seq before the checkpoint, `state_hash` the
  fold through it, `prev_hash` the chain (see the fold rules below);
  `parked.checkpoint_seq` is that checkpoint's seq. Nothing is written on a
  park resume. Note the fold keeps `parked_at` until the next `settle`.
- After a death resume: `clock{host_hello}` (from the resume hello's `at`),
  `error{transient, retry: true, ref: turn_id}` for a turn whose response was
  lost, `op_state{lost}` + `tool_result{content: "error: operation lost (VM
  restarted)"}` per open call, then `wake{tool_result}` and the next turn. Never
  a second `session_start`.
- `bulk{kind: spill}.path` and `tool_result.spill_ref` are the bulk path
  `.spill/<call_id>.txt` (relative), the file the host holds.
- `settle.manifest` is the workspace manifest
  `{"version": 1, "files": [{path, bytes, sha256, mode}], "total_bytes"}`
  (`protocol/bulk.py`), `{}` when the guest has no workspace.

## Log rules (`validate_log`)

Per-event rules (`validate_event`) run on every event first. The whole-log rules
below are applied only to events that passed them, so a malformed event produces its
schema errors and nothing else. `validate_log` needs only the ids seen so far (cheap set
lookups); every rule that needs the folded state belongs to the fold (next section) so
that each rule has exactly one owner.

1. The log is non-empty. The first event is `session_start`, and there is exactly one.
2. `seq` is exactly `1, 2, 3, ...` in list order: no gaps, no repeats, no reordering.
3. Every event carries the same `session_id`.
4. `memory.derived_from` names an earlier `tool_call.call_id` or an earlier
   `compaction.summary_turn_id`.
5. `checkpoint.through_seq` is `< seq`.
6. `settle.input_id` names an earlier `input`.
7. Unknown `type` or `schema` is an error; nothing is skipped silently.

## Rules the fold checks (`protocol/fold.py`)

`fold.apply` runs `validate_event` on every event it applies, then the handlers check the
references that need the folded state. Each is a `FoldError("seq N: ...")`; `replay`
reports them next to the `validate_log` ones (docs/REPLAY.md).

- `turn_start.includes_through_seq` is `< seq` and `>=` the previous turn's value;
  `turn_id` is unique.
- `llm_response.turn_id` and `tool_call.turn_id` name an earlier `turn_start`; at most one
  `llm_response` per turn.
- `tool_call.(name, side)` appears in `session_start.tools` (the allowlist; a side mismatch
  is an error); `call_id` is unique.
- `op_state.call_id`, `tool_result.call_id` and `question.call_id` name an earlier
  `tool_call`; `question_id` is unique.
- `tool_result.late` is exactly `(the call already has a result) or (its placeholder was
  committed by a turn)`. A second result is counted and ignored; a first result after a
  committed placeholder is a new item; a `late: true` that is neither is an error.
- `input{dedupe: new}` with an `input_id` seen before is an error; `ui_response` names an
  open, unanswered `question_id`.
- `compaction.summary_turn_id` names a `turn_start` of type `compaction` whose
  `llm_response` already appeared; `range[1]` is below that turn's `seq`.
- `parked.checkpoint_seq` is the `seq` of an earlier `checkpoint`.
- The checkpoint chain (`verify_checkpoints`): `through_seq` never goes backwards; the first
  checkpoint's `prev_hash` equals `hash_value(session_start event)` (canonical sha256 of the
  whole envelope); each later one's `prev_hash` equals the previous checkpoint's
  `state_hash`; every `state_hash` equals the fold through its `through_seq`.

Not verified anywhere yet: that `turn_id`, `question_id` and other derived ids equal
`derive_id(...)`; `guard_decision.ref` targets (the guard runs before the submit, so
the `tool_call` it refers to is appended later); `wake` id lists.

## How `validate_log` reports

`validate_log(events) -> list[str]`. An empty list means the log is valid. Every entry
is `"seq N: message"` where `N` is the offending event's `seq` (or its 1-based position
when `seq` is missing or not an int; `seq 0` for an empty or non-list log). Per-event
messages keep their `key: problem` form after the prefix, e.g.
`seq 7: body.state: 'done' not in ['RUNNING']`. The function collects every violation
rather than stopping at the first, and never raises. `log_problems(events)` is the same
list before formatting, as `(seq, message)` pairs; `fold.replay` merges it with the
fold's own problems and sorts by seq.
