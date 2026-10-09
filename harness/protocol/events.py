"""events — event schema and validation (PLAN.md section 5, 12; docs/EVENTS.md).

Public contract (other modules import these names):
  SCHEMA_VERSION = 1
  EVENT_TYPES: frozenset of the 18 type names in PLAN.md section 5
  REQUIRED_BODY / OPTIONAL_BODY / ENUMS: the per-type body schema
  MIN_INT / STR_LISTS / OPEN_ENUMS: the per-type shape rules beyond types
  TERMINAL_OP_STATES, OP_INPUT_REQUIRED, UNANSWERED
  make_event(session_id, seq, ts, type, body) -> dict
  validate_event(event) -> list[str]        empty list == valid; never raises
  validate_log(events) -> list[str]         whole-log rules; never raises
  log_problems(events) -> list[(seq, msg)]  validate_log before formatting

Every event is an envelope {session_id, seq, ts, schema, type, body}. `ts` is
data (an ISO-8601 string), never a fold input. Bodies must be canonicalisable
(see canonical.py): string keys, no NaN/infinity, so that checkpoints and derived
ids can hash them.

Who checks what: validate_event owns every rule that one event decides on its
own (envelope, body keys and types, enums, ranges). validate_log adds the
whole-log rules that need only the set of ids seen so far. Rules that depend
on the folded state (a turn exists and has one response, a call is open, a
tool was offered, includes_through_seq never goes backwards, the checkpoint
chain) live in protocol.fold, which calls validate_event on every event it
applies, so each rule has exactly one owner.

Everything here is pure: no I/O, no clock, no randomness.
"""
from __future__ import annotations

from protocol import SCHEMA_VERSION  # noqa: F401
from protocol.allowlist import SIDES, tools_errors
from protocol.canonical import canonicalisable_error, is_int

EVENT_TYPES = frozenset({
    "session_start", "input", "clock", "turn_start", "llm_response",
    "tool_call", "guard_decision", "op_state", "tool_result", "wake",
    "question", "parked", "compaction", "memory", "bulk", "checkpoint",
    "error", "settle",
})

# guard_decision.reason values (PLAN.md section 6); reason is free text, not an
# enum, but the fold counts this one.
UNANSWERED = "unanswered"

# ---------------------------------------------------------------- type specs
# A spec is a tuple of accepted Python types. bool is never accepted where
# only int is listed (bool is a subclass of int in Python).
_NONE = type(None)
STR = (str,)
INT = (int,)
BOOL = (bool,)
DICT = (dict,)
LIST = (list,)
OPT_STR = (str, _NONE)
DICT_OR_LIST = (dict, list)

# envelope: key -> (type spec, extra check returning a message or None)
ENVELOPE: dict[str, tuple] = {
    "session_id": (STR, lambda v: None if v else "expected non-empty str"),
    "seq": (INT, lambda v: None if v >= 1 else f"must be >= 1, got {v}"),
    "ts": (STR, None),
    "schema": (INT, lambda v: None if v == SCHEMA_VERSION
               else f"unknown version {v} (expected {SCHEMA_VERSION})"),
    "type": (STR, lambda v: None if v in EVENT_TYPES else f"unknown event type {v!r}"),
    "body": (DICT, None),
}
ENVELOPE_KEYS = tuple(ENVELOPE)

REQUIRED_BODY: dict[str, dict[str, tuple]] = {
    "session_start": {
        "user": STR, "tenant": STR, "workflow_run_id": STR,
        "config_hash": STR, "system_hash": STR, "tools": LIST,
        "harness_version": STR, "image_version": STR, "resumed": BOOL,
    },
    "input": {"input_id": STR, "kind": STR, "text": STR, "dedupe": STR},
    "clock": {"kind": STR, "at": INT},
    "turn_start": {
        "turn_id": STR, "type": STR, "includes_through_seq": INT,
        "model": STR, "params": DICT, "request_hash": STR,
        "preamble_hash": STR, "workspace_tree_id": OPT_STR,
    },
    "llm_response": {
        "turn_id": STR, "message": DICT, "finish_reason": STR, "usage": DICT,
    },
    "tool_call": {
        "turn_id": STR, "call_id": STR, "name": STR, "side": STR,
        "args": DICT, "state": STR,
    },
    "guard_decision": {
        "ref": STR, "decision": STR, "reason": STR, "policy_id": STR,
    },
    "op_state": {"call_id": STR, "state": STR},
    "tool_result": {"call_id": STR, "content": STR},
    "wake": {"reason": STR, "result_ids": LIST, "input_ids": LIST},
    "question": {
        "question_id": STR, "call_id": STR, "text": STR, "options": LIST,
        "deadline": INT,
    },
    "parked": {"checkpoint_seq": INT, "reason": STR},
    "compaction": {
        "range": LIST, "summary_turn_id": STR, "tokens_before": INT,
        "tokens_after": INT,
    },
    "memory": {
        "scope": STR, "op": STR, "slug": STR, "sha256": STR,
        "derived_from": STR,
    },
    "bulk": {"kind": STR, "path": STR, "sha256": STR, "bytes": INT},
    "checkpoint": {"through_seq": INT, "state_hash": STR, "prev_hash": STR},
    "error": {"class": STR, "message": STR, "retry": BOOL},
    "settle": {
        "input_id": STR, "outcome": STR, "answer": STR, "usage": DICT,
        "manifest": DICT_OR_LIST,
    },
}

OPTIONAL_BODY: dict[str, dict[str, tuple]] = {
    "input": {"question_id": STR, "answer": STR},
    "llm_response": {"response_id": OPT_STR},
    # M4: `attempt` = the gateway POST of an mcp call (0 = the first);
    # `input_request` {request_state, schema} only with state input_required
    "op_state": {"duration_ms": INT, "pid": INT, "side_ref": STR, "attempt": INT,
                 "input_request": DICT},
    "tool_result": {"spill_ref": OPT_STR, "rewrite": OPT_STR, "late": BOOL},
    "error": {"ref": STR},
    # M5: the question a `parked{reason: "question"}` waits on (D12), and the
    # task's wall clock used up to the park (ms; absent when no task runs)
    "parked": {"question_id": STR, "wall_used_ms": INT},
}

ENUMS: dict[str, dict[str, frozenset]] = {
    "input": {
        "kind": frozenset({"task", "follow_up", "steer", "ui_response"}),
        "dedupe": frozenset({"new", "duplicate", "late"}),
    },
    "turn_start": {"type": frozenset({"regular", "compaction"})},
    "tool_call": {"side": frozenset(SIDES), "state": frozenset({"RUNNING"})},
    "guard_decision": {"decision": frozenset({"allow", "deny", "ask"})},
    "op_state": {"state": frozenset({
        "queued", "running", "input_required", "done", "failed", "timeout", "lost", "killed",
    })},
    "wake": {"reason": frozenset({
        "input", "tool_result", "grace", "idle", "question",
    })},
    "memory": {
        "scope": frozenset({"user", "session"}),
        "op": frozenset({"write", "remove"}),
    },
    "bulk": {"kind": frozenset({"spill", "workspace"})},
    "error": {"class": frozenset({
        "transient", "overflow", "budget", "limit", "protocol", "fatal",
    })},
    "settle": {"outcome": frozenset({"done", "failed", "timeout", "escalated", "aborted"})},
}

# clock.kind: a fixed set plus the open `grace_*` family (PLAN.md section 5).
CLOCK_KINDS = frozenset({
    "host_hello", "host_heartbeat", "turn_clock", "idle_timer",
    "question_deadline", "wall_clock",
})
CLOCK_KIND_PREFIXES = ("grace_",)

# open enums: a fixed set plus prefix families. {type: {key: (fixed, prefixes)}}
OPEN_ENUMS: dict[str, dict[str, tuple[frozenset, tuple[str, ...]]]] = {
    "clock": {"kind": (CLOCK_KINDS, CLOCK_KIND_PREFIXES)},
}

# int lower bounds, checked only when the value is an int. {type: {key: minimum}}
MIN_INT: dict[str, dict[str, int]] = {
    "turn_start": {"includes_through_seq": 0},
    "question": {"deadline": 0},
    "compaction": {"tokens_before": 0, "tokens_after": 0},
    "op_state": {"duration_ms": 0, "attempt": 0},
    "bulk": {"bytes": 0},
    "checkpoint": {"through_seq": 1},
    "parked": {"checkpoint_seq": 1, "wall_used_ms": 0},
}

# list keys whose items must all be str. {type: (key, ...)}
STR_LISTS: dict[str, tuple[str, ...]] = {
    "wake": ("result_ids", "input_ids"),
    "question": ("options",),
}

TERMINAL_OP_STATES = frozenset({"done", "failed", "timeout", "lost", "killed"})
# M4: an mcp op suspended on a gateway input request (not terminal: the
# re-send after the answer is op_state{running, attempt + 1})
OP_INPUT_REQUIRED = "input_required"


# ---------------------------------------------------------------- helpers
def _is(value, spec: tuple) -> bool:
    if isinstance(value, bool):
        return bool in spec
    return isinstance(value, spec)


def _spec_name(spec: tuple) -> str:
    return " | ".join("None" if t is _NONE else t.__name__ for t in spec)


def make_event(session_id: str, seq: int, ts: str, type: str, body: dict) -> dict:
    """Build an envelope. Does not validate; call validate_event for that."""
    return {
        "session_id": session_id,
        "seq": seq,
        "ts": ts,
        "schema": SCHEMA_VERSION,
        "type": type,
        "body": body,
    }


# ---------------------------------------------------------------- per-event
def _validate_session_start_tools(tools, errors: list[str]) -> None:
    errors.extend(f"body.{e}" for e in tools_errors(tools))


def _validate_compaction_range(rng, errors: list[str]) -> None:
    if not isinstance(rng, list):
        return
    if len(rng) != 2 or not all(is_int(v) for v in rng):
        errors.append("body.range: expected [from_seq, to_seq] of two ints")
    elif rng[0] < 1 or rng[0] > rng[1]:
        errors.append(f"body.range: need 1 <= from_seq <= to_seq, got {rng}")


def _validate_input_request(body: dict, errors: list[str]) -> None:
    """op_state.input_request: present exactly when state is input_required,
    {request_state: non-empty str, schema: dict}."""
    request = body.get("input_request")
    if body.get("state") != OP_INPUT_REQUIRED:
        if request is not None:
            errors.append(f"body.input_request: only with state {OP_INPUT_REQUIRED!r}")
        return
    if request is None:
        errors.append(f"body.input_request: missing (state {OP_INPUT_REQUIRED!r})")
    elif isinstance(request, dict):
        if not isinstance(request.get("request_state"), str) or not request["request_state"]:
            errors.append("body.input_request.request_state: expected non-empty str")
        if not isinstance(request.get("schema"), dict):
            errors.append("body.input_request.schema: expected dict")


def _validate_body_extras(etype: str, body: dict, errors: list[str]) -> None:
    """Shape rules beyond key presence/type/enum, driven by the tables above."""
    for key, minimum in MIN_INT.get(etype, {}).items():
        if is_int(body.get(key)) and body[key] < minimum:
            errors.append(f"body.{key}: must be >= {minimum}")
    for key in STR_LISTS.get(etype, ()):
        value = body.get(key)
        if isinstance(value, list) and not all(isinstance(v, str) for v in value):
            errors.append(f"body.{key}: expected list of str")
    for key, (fixed, prefixes) in OPEN_ENUMS.get(etype, {}).items():
        value = body.get(key)
        if isinstance(value, str) and value not in fixed and not value.startswith(prefixes):
            family = " or ".join(p + "*" for p in prefixes)
            errors.append(f"body.{key}: {value!r} not in {sorted(fixed)} or {family}")
    if etype == "session_start":
        _validate_session_start_tools(body.get("tools"), errors)
    elif etype == "compaction":
        _validate_compaction_range(body.get("range"), errors)
    elif etype == "op_state":
        _validate_input_request(body, errors)


def validate_event(event) -> list[str]:
    """Envelope + body schema of one event. Returns [] when valid. Never raises."""
    errors: list[str] = []
    if not isinstance(event, dict):
        return [f"event: expected dict, got {type(event).__name__}"]

    # envelope
    for key, (spec, check) in ENVELOPE.items():
        if key not in event:
            errors.append(f"{key}: missing")
        elif not _is(event[key], spec):
            errors.append(f"{key}: expected {_spec_name(spec)}, got {type(event[key]).__name__}")
        elif check is not None and (problem := check(event[key])):
            errors.append(f"{key}: {problem}")
    for key in event:
        if key not in ENVELOPE:
            errors.append(f"{key}: unknown envelope key")

    etype, body = event.get("type"), event.get("body")
    if not isinstance(body, dict) or etype not in EVENT_TYPES:
        return errors

    # body: required keys and types
    for key, spec in REQUIRED_BODY[etype].items():
        if key not in body:
            errors.append(f"body.{key}: missing")
        elif not _is(body[key], spec):
            errors.append(
                f"body.{key}: expected {_spec_name(spec)}, got {type(body[key]).__name__}")
    for key, spec in OPTIONAL_BODY.get(etype, {}).items():
        if key in body and not _is(body[key], spec):
            errors.append(
                f"body.{key}: expected {_spec_name(spec)}, got {type(body[key]).__name__}")
    # enums
    for key, allowed in ENUMS.get(etype, {}).items():
        if key in body and isinstance(body[key], str) and body[key] not in allowed:
            errors.append(f"body.{key}: {body[key]!r} not in {sorted(allowed)}")
    _validate_body_extras(etype, body, errors)

    # canonicalisable: the body will be hashed by checkpoints and derived ids
    problem = canonicalisable_error(body)
    if problem:
        errors.append(f"body: not canonicalisable ({problem})")
    return errors


# ---------------------------------------------------------------- whole log
def log_problems(events) -> list[tuple[int, str]]:
    """validate_log before formatting: (seq, message) pairs in log order.
    `seq` is the offending event's seq, or its 1-based position when seq is
    missing or not an int, or 0 for an empty or non-list log."""
    problems: list[tuple[int, str]] = []
    if not isinstance(events, (list, tuple)):
        return [(0, f"expected a list of events, got {type(events).__name__}")]
    if not events:
        return [(0, "empty log (first event must be session_start)")]

    # ids seen so far (cheap set lookups; state-dependent rules are the fold's)
    session_id = None
    input_ids: set[str] = set()
    call_ids: set[str] = set()
    summaries: set[str] = set()                  # compaction summary_turn_ids

    for index, event in enumerate(events):
        expected_seq = index + 1
        seq = event.get("seq") if isinstance(event, dict) else None
        label = seq if is_int(seq) else expected_seq
        ev_errors = validate_event(event)
        problems.extend((label, e) for e in ev_errors)

        # gapless, strictly increasing seq from 1
        if is_int(seq) and seq != expected_seq:
            problems.append((seq, f"expected seq {expected_seq} (gapless, increasing from 1)"))

        if ev_errors:
            continue  # cross-reference rules need a well-formed event

        etype, body = event["type"], event["body"]

        # one session_id
        if session_id is None:
            session_id = event["session_id"]
        elif event["session_id"] != session_id:
            problems.append((seq, f"session_id {event['session_id']!r} != {session_id!r}"))

        # first event is session_start, and only one
        if index == 0:
            if etype != "session_start":
                problems.append((seq, f"first event must be session_start, got {etype!r}"))
        elif etype == "session_start":
            problems.append((seq, "second session_start (only one allowed)"))
            continue

        if etype == "input":
            input_ids.add(body["input_id"])
        elif etype == "tool_call":
            call_ids.add(body["call_id"])
        elif etype == "compaction":
            summaries.add(body["summary_turn_id"])
        elif etype == "memory":
            src = body["derived_from"]
            if src not in call_ids and src not in summaries:
                problems.append((seq, f"memory.derived_from {src!r} names no tool_call "
                                      f"call_id or compaction summary_turn_id"))
        elif etype == "checkpoint":
            if body["through_seq"] >= seq:
                problems.append((seq, f"checkpoint.through_seq {body['through_seq']} "
                                      f"must be < seq {seq}"))
        elif etype == "settle":
            if body["input_id"] not in input_ids:
                problems.append((seq, f"settle.input_id {body['input_id']!r} names no input"))

    return problems


def validate_log(events) -> list[str]:
    """Whole-log rules (docs/EVENTS.md). Each violation is 'seq N: message'.
    Per-event errors from validate_event are included with the same prefix.
    Cross-reference rules are only applied to events that pass validate_event.
    Never raises."""
    return [f"seq {seq}: {msg}" for seq, msg in log_problems(events)]
