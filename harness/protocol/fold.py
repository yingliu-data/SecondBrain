"""fold — fold(events) -> State, pure (docs/REPLAY.md).

Public contract (other modules import these names):
  class FoldError(ValueError)              .seq = the offending seq; str() = "seq N: ..."
  class State                              dataclass; to_canonical() -> plain dict
  apply(state, event) -> State             one step of the fold, in place
  apply_valid(state, event) -> State       the same for an event already validated
  fold(events, upto_seq=None, *, state=None) -> State
                                           applies events in seq order; with `state`
                                           resumes from a kept State (seq <= last_seq
                                           skipped)
  state_hash(state) -> str                 hash_value(state.to_canonical())
  checkpoint_body(events, prev=None, *, state=None) -> dict
                                           {through_seq, state_hash, prev_hash} sealing
                                           the events (prev: the chain's head as they
                                           have it)
  verify_checkpoints(events, *, last_only=False) -> list[str]
                                           "seq N: ..." strings, empty == all good
  replay(events) -> (State, list[str])     validate_log + verify_checkpoints + fold
  turn_inputs(events, turn_id)             the chat messages that turn saw
  PLACEHOLDER                              content of a placeholder tool message
  TASK_INPUT_KINDS                         input kinds that start a task when none runs

Index fields (State, never hashed, never compared; derived from the events
like the rest, so a fold of the same log rebuilds them): `call_args`
(call_id -> the tool_call's args), `question_text` (question_id -> {text,
options}), `settles_by_input` (input_id -> the settle body), `open_input`
(the input_id of the task that has no settle yet: the first new
task/follow_up/steer input after a settle or the run's start; None between
tasks). The host reads them instead of re-deriving them from the log.

Purity: this module never reads the clock, the environment, randomness or a
file, and never calls the LLM, a tool or the gateway. `ts` is data; it is never
read here. Time inside the fold comes from `clock` events only.

Who checks what: apply() runs events.validate_event on every event, so the
handlers below never re-check shapes or enums. They do state transitions and
the reference rules that need the folded state (a turn exists and has one
response, a call is open, a tool was offered, includes_through_seq never goes
backwards, a parked checkpoint exists). verify_checkpoints owns the checkpoint
hash and prev_hash chain.

Message shapes in the prefix (what the model has seen):
  user      {role, content, input_id, kind, preamble}   preamble = preamble_hash of
                                                         the turn that committed it
                                                         (first user message of the
                                                         batch), else None
  assistant the llm_response.message dict, verbatim
  tool      {role: "tool", tool_call_id, content}         content = rewrite or content

Ordering key: every item has an `origin` seq. A user message's origin is its
input seq, an assistant message's is its llm_response seq, a tool message's is
the seq of its tool_call (so results that arrive in swapped order still fold to
the same prefix: invariant 5). A result that lands after its placeholder was
committed is a NEW item with origin = its own seq. A compaction summary takes
origin = range[0], the position of the range it replaces.
"""
from __future__ import annotations

import bisect
import copy
from dataclasses import dataclass, field, fields

from protocol.allowlist import is_allowed
from protocol.canonical import hash_value, is_int
from protocol.events import (
    OP_INPUT_REQUIRED, TERMINAL_OP_STATES, UNANSWERED, log_problems, validate_event,
)

PLACEHOLDER = "[pending]"
# the input kinds that start a task when none runs (guest.coordinator.INPUT_KINDS)
TASK_INPUT_KINDS = ("task", "follow_up", "steer")


class FoldError(ValueError):
    """A structural impossibility in the log. str() is 'seq N: message'."""

    def __init__(self, seq: int, message: str):
        super().__init__(f"seq {seq}: {message}")
        self.seq = seq
        self.message = message


# index fields: derived from the events, never emitted, never hashed, never compared
_INDEX_FIELDS = ("_unresolved", "_checkpoint_seqs", "call_args", "question_text",
                 "settles_by_input", "open_input")


@dataclass
class State:
    # fixed at session_start
    user: str | None = None
    tenant: str | None = None
    session_id: str | None = None
    config_hash: str | None = None
    system_hash: str | None = None
    tools: list = field(default_factory=list)
    # context: items are {seq, origin, message}; committed is in commit order,
    # staged_suffix sorted by (origin, seq)
    committed: list = field(default_factory=list)
    staged_suffix: list = field(default_factory=list)
    open_calls: dict = field(default_factory=dict)         # call_id -> {name, side, turn_id, state, seq, result?,
                                                           #   attempt?, input_request? (mcp ops)}
    placeholders: set = field(default_factory=set)         # call_ids shown as placeholders
    queues: dict = field(default_factory=lambda: {"follow_up": [], "steer": []})  # kind -> [seq]
    open_questions: dict = field(default_factory=dict)     # question_id -> {deadline, call_id, answered, seq}
    denied_questions: int = 0
    session_memory: dict = field(default_factory=dict)     # slug -> sha256
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    limits_consumed: dict = field(default_factory=lambda: {"turns": 0, "ops": 0})
    last_settle: dict | None = None
    parked_at: int | None = None                           # checkpoint_seq of the park, None when not parked
    seen_input_ids: set = field(default_factory=set)
    clock_at: int | None = None
    compacted_through: int | None = None
    turns: dict = field(default_factory=dict)              # turn_id -> {turn_id, seq, includes_through_seq, type, ...}
    counters: dict = field(default_factory=lambda: {
        "inputs": 0, "duplicates": 0, "late_inputs": 0, "wakes": 0,
        "errors": 0, "bulk": 0, "bulk_bytes": 0, "checkpoints": 0,
        "guard_allow": 0, "guard_deny": 0, "guard_ask": 0,
        "late_results": 0, "placeholder_fills": 0, "user_memory": 0,
    })
    last_seq: int = 0
    # indexes (not hashed): calls without a result; seqs of checkpoint events
    _unresolved: set = field(default_factory=set, repr=False, compare=False)
    _checkpoint_seqs: set = field(default_factory=set, repr=False, compare=False)
    # host indexes (not hashed; see the module docstring)
    call_args: dict = field(default_factory=dict, repr=False, compare=False)
    question_text: dict = field(default_factory=dict, repr=False, compare=False)
    settles_by_input: dict = field(default_factory=dict, repr=False, compare=False)
    open_input: str | None = field(default=None, repr=False, compare=False)

    def to_canonical(self) -> dict:
        """A plain dict (sets as sorted lists) hashable with canonical.hash_value.
        The index fields are left out (and never copied)."""
        out = {f.name: copy.deepcopy(getattr(self, f.name))
               for f in fields(self) if f.name not in _INDEX_FIELDS}
        committed = out.pop("committed")
        out["committed_prefix"] = [it["message"] for it in committed]
        out["prefix_origins"] = [it["origin"] for it in committed]
        out["parked"] = self.parked
        out["placeholders"] = sorted(self.placeholders)
        out["seen_input_ids"] = sorted(self.seen_input_ids)
        out["turns"] = list(out["turns"].values())
        return out

    # convenience views (not part of the hash)
    @property
    def committed_prefix(self) -> list:
        """The chat messages the model has seen, in order."""
        return [it["message"] for it in self.committed]

    @property
    def prefix_origins(self) -> list:
        return [it["origin"] for it in self.committed]

    @property
    def parked(self) -> bool:
        return self.parked_at is not None

    def last_turn(self) -> dict | None:
        return next(reversed(self.turns.values())) if self.turns else None


# ---------------------------------------------------------------- helpers
def _order_key(item: dict) -> tuple:
    return item["origin"], item["seq"]


def _stage(state: State, item: dict) -> None:
    """Insert into staged_suffix keeping it sorted by (origin, seq)."""
    bisect.insort(state.staged_suffix, item, key=_order_key)


def _user_message(body: dict, kind: str) -> dict:
    return {
        "role": "user",
        "content": body["text"],
        "input_id": body["input_id"],
        "kind": kind,
        "preamble": None,
    }


def _call(state: State, seq: int, call_id: str, what: str) -> dict:
    call = state.open_calls.get(call_id)
    if call is None:
        raise FoldError(seq, f"{what} for unknown call {call_id!r}")
    return call


# ---------------------------------------------------------------- per-type
def _session_start(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    if seq != 1:
        raise FoldError(seq, "session_start must be the first event (seq 1)")
    state.session_id = event["session_id"]
    state.user = body["user"]
    state.tenant = body["tenant"]
    state.config_hash = body["config_hash"]
    state.system_hash = body["system_hash"]
    state.tools = [{"name": t["name"], "side": t["side"], "pin": t["pin"]} for t in body["tools"]]


def _input(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    input_id, kind, dedupe = body["input_id"], body["kind"], body["dedupe"]
    state.counters["inputs"] += 1
    # dedupe first, whatever the kind (invariant 4)
    if dedupe != "new":
        state.seen_input_ids.add(input_id)
        state.counters["duplicates" if dedupe == "duplicate" else "late_inputs"] += 1
        return
    if input_id in state.seen_input_ids:
        raise FoldError(seq, f"input_id {input_id!r} seen before but dedupe is 'new'")
    state.seen_input_ids.add(input_id)
    if state.open_input is None and kind in TASK_INPUT_KINDS:
        state.open_input = input_id

    if kind == "ui_response":
        qid = body.get("question_id")
        if qid is None:
            raise FoldError(seq, "ui_response without question_id")
        q = state.open_questions.get(qid)
        if q is None:
            raise FoldError(seq, f"ui_response for unknown question {qid!r}")
        if q["answered"]:
            raise FoldError(seq, f"question {qid!r} answered twice")
        q["answered"] = True
        q["answer"] = body.get("answer")
        q["answered_seq"] = seq
        return
    # task / follow_up / steer: a staged user message; the latter two also queued
    _stage(state, {"seq": seq, "origin": seq, "message": _user_message(body, kind)})
    if kind in state.queues:
        state.queues[kind].append(seq)


def _clock(state: State, event: dict) -> None:
    state.clock_at = event["body"]["at"]


def _turn_start(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    turn_id, its = body["turn_id"], body["includes_through_seq"]
    if its >= seq:
        raise FoldError(seq, f"includes_through_seq {its!r} must be in 0..{seq - 1}")
    if turn_id in state.turns:
        raise FoldError(seq, f"duplicate turn_id {turn_id!r}")
    last = state.last_turn()
    if last is not None and its < last["includes_through_seq"]:
        raise FoldError(seq, "includes_through_seq goes backwards")

    # everything with seq <= includes_through_seq is what this turn saw
    batch = [it for it in state.staged_suffix if it["seq"] <= its]
    state.staged_suffix = [it for it in state.staged_suffix if it["seq"] > its]
    for queue in state.queues.values():
        queue[:] = [s for s in queue if s > its]
    # plus a placeholder for each call still open when the turn was assembled (async mode)
    for cid in state._unresolved:
        call = state.open_calls[cid]
        if cid not in state.placeholders and call["seq"] <= its:
            state.placeholders.add(cid)
            batch.append({"seq": seq, "origin": call["seq"], "message": {
                "role": "tool", "tool_call_id": cid, "content": PLACEHOLDER}})
    batch.sort(key=_order_key)

    first_user = True
    for it in batch:
        msg = it["message"]
        if msg.get("role") == "user" and "preamble" in msg:
            msg = dict(msg)
            msg["preamble"] = body["preamble_hash"] if first_user else None
            first_user = False
        state.committed.append({"seq": it["seq"], "origin": it["origin"], "message": msg})

    state.turns[turn_id] = {
        "turn_id": turn_id,
        "seq": seq,
        "includes_through_seq": its,
        "type": body["type"],
        "model": body["model"],
        "request_hash": body["request_hash"],
        "preamble_hash": body["preamble_hash"],
        "workspace_tree_id": body["workspace_tree_id"],
        "response_seq": None,
    }
    state.limits_consumed["turns"] += 1


def _llm_response(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    turn_id = body["turn_id"]
    turn = state.turns.get(turn_id)
    if turn is None:
        raise FoldError(seq, f"llm_response for unknown turn {turn_id!r}")
    if turn["response_seq"] is not None:
        raise FoldError(seq, f"second llm_response for turn {turn_id!r}")
    turn["response_seq"] = seq
    turn["finish_reason"] = body["finish_reason"]
    _stage(state, {"seq": seq, "origin": seq, "message": body["message"]})
    for k in ("prompt_tokens", "completion_tokens"):
        v = body["usage"].get(k, 0)
        if is_int(v):
            state.usage[k] += v


def _tool_call(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    turn_id, call_id, name, side = body["turn_id"], body["call_id"], body["name"], body["side"]
    if turn_id not in state.turns:
        raise FoldError(seq, f"tool_call for unknown turn {turn_id!r}")
    if call_id in state.open_calls:
        raise FoldError(seq, f"duplicate call_id {call_id!r}")
    if not is_allowed(state.tools, name, side):
        raise FoldError(seq, f"tool {name!r} side {side!r} not offered in session_start.tools")
    state.open_calls[call_id] = {
        "name": name, "side": side, "turn_id": turn_id, "state": body["state"], "seq": seq,
    }
    state.call_args[call_id] = body["args"]
    state._unresolved.add(call_id)
    state.limits_consumed["ops"] += 1


def _guard_decision(state: State, event: dict) -> None:
    body = event["body"]
    state.counters[f"guard_{body['decision']}"] += 1
    if body["decision"] == "deny" and body["reason"] == UNANSWERED:
        state.denied_questions += 1


def _op_state(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    call = _call(state, seq, body["call_id"], "op_state")
    suspended = body["state"] == OP_INPUT_REQUIRED
    if suspended and call["side"] != "mcp":
        raise FoldError(seq, f"op_state {OP_INPUT_REQUIRED} for {call['side']}-side call "
                             f"{body['call_id']!r} (mcp calls only)")
    call["state"] = body["state"]
    if "duration_ms" in body:
        call["duration_ms"] = body["duration_ms"]
    if "attempt" in body:
        call["attempt"] = body["attempt"]
    # a suspended mcp op carries what its re-send needs; any other state clears it
    if suspended:
        call["input_request"] = body["input_request"]
    else:
        call.pop("input_request", None)


def _tool_result(state: State, event: dict) -> None:
    """The one 'late' rule: a result is late when the call already has a
    result or its placeholder was committed, and body.late must say so.
    A second result is counted and dropped; a first result after a committed
    placeholder is a NEW item (origin = its own seq); any other result takes
    the call's position (origin = the tool_call's seq)."""
    seq, body = event["seq"], event["body"]
    call_id = body["call_id"]
    call = _call(state, seq, call_id, "tool_result")
    flagged = body.get("late", False)
    has_result = "result" in call
    after_placeholder = call_id in state.placeholders
    if (has_result or after_placeholder) != flagged:
        why = ("already has a result" if has_result
               else "lands after its committed placeholder" if after_placeholder
               else "has no earlier result or committed placeholder")
        raise FoldError(seq, f"tool_result for call {call_id!r} {why} but late is {flagged}")
    if has_result:
        state.counters["late_results"] += 1   # terminal call: recorded, ignored
        return
    content = body["rewrite"] if body.get("rewrite") is not None else body["content"]
    call["result"] = seq
    if call["state"] not in TERMINAL_OP_STATES:
        call["state"] = "done"
    if body.get("spill_ref") is not None:
        call["spill_ref"] = body["spill_ref"]
    state._unresolved.discard(call_id)
    message = {"role": "tool", "tool_call_id": call_id, "content": content}
    if after_placeholder:
        # the model already saw a placeholder: a NEW item, the prefix is untouched
        state.counters["placeholder_fills"] += 1
        _stage(state, {"seq": seq, "origin": seq, "message": message})
    else:
        _stage(state, {"seq": seq, "origin": call["seq"], "message": message})


def _question(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    qid = body["question_id"]
    _call(state, seq, body["call_id"], "question")
    if qid in state.open_questions:
        raise FoldError(seq, f"duplicate question_id {qid!r}")
    state.open_questions[qid] = {
        "deadline": body["deadline"], "call_id": body["call_id"], "answered": False, "seq": seq,
    }
    state.question_text[qid] = {"text": body["text"], "options": body["options"]}


def _parked(state: State, event: dict) -> None:
    seq, cp = event["seq"], event["body"]["checkpoint_seq"]
    if cp not in state._checkpoint_seqs:
        raise FoldError(seq, f"parked.checkpoint_seq {cp} is not the seq of an earlier checkpoint")
    state.parked_at = cp


def _compaction(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    frm, to = body["range"]
    summary_turn_id = body["summary_turn_id"]
    turn = state.turns.get(summary_turn_id)
    if turn is None:
        raise FoldError(seq, f"compaction summary turn {summary_turn_id!r} unknown")
    if turn["type"] != "compaction":
        raise FoldError(seq, f"compaction summary turn {summary_turn_id!r} is a "
                             f"{turn['type']!r} turn, not a compaction turn")
    if turn["response_seq"] is None:
        raise FoldError(seq, f"compaction summary turn {summary_turn_id!r} has no llm_response")
    if to >= turn["seq"]:
        raise FoldError(seq, f"compaction.range {body['range']} must cover only seqs below "
                             f"the compaction turn (seq {turn['seq']})")
    resp_seq = turn["response_seq"]

    summary = None
    for it in state.staged_suffix:
        if it["seq"] == resp_seq:
            summary = it
    if summary is None:
        for it in state.committed:
            if it["origin"] == resp_seq:
                summary = it
    if summary is None:
        raise FoldError(seq, f"compaction summary message (seq {resp_seq}) not found")

    before = [it for it in state.committed if it["origin"] < frm]
    after = [it for it in state.committed if it["origin"] > to and it["origin"] != resp_seq]
    state.committed = before + [{"seq": resp_seq, "origin": frm, "message": summary["message"]}] + after
    state.staged_suffix = [
        it for it in state.staged_suffix
        if it["seq"] != resp_seq and not (frm <= it["origin"] <= to)
    ]
    state.compacted_through = to


def _memory(state: State, event: dict) -> None:
    body = event["body"]
    if body["scope"] == "user":
        state.counters["user_memory"] += 1
    elif body["op"] == "write":
        state.session_memory[body["slug"]] = body["sha256"]
    else:
        state.session_memory.pop(body["slug"], None)


def _bulk(state: State, event: dict) -> None:
    state.counters["bulk"] += 1
    state.counters["bulk_bytes"] += event["body"]["bytes"]


def _checkpoint(state: State, event: dict) -> None:
    state.counters["checkpoints"] += 1
    state._checkpoint_seqs.add(event["seq"])


def _settle(state: State, event: dict) -> None:
    seq, body = event["seq"], event["body"]
    state.last_settle = {"seq": seq, "input_id": body["input_id"], "outcome": body["outcome"]}
    state.parked_at = None
    state.settles_by_input[body["input_id"]] = body
    state.open_input = None


_HANDLERS = {
    "session_start": _session_start,
    "input": _input,
    "clock": _clock,
    "turn_start": _turn_start,
    "llm_response": _llm_response,
    "tool_call": _tool_call,
    "guard_decision": _guard_decision,
    "op_state": _op_state,
    "tool_result": _tool_result,
    "question": _question,
    "parked": _parked,
    "compaction": _compaction,
    "memory": _memory,
    "bulk": _bulk,
    "checkpoint": _checkpoint,
    "settle": _settle,
}
# types that only bump a counter
_COUNTED = {"wake": "wakes", "error": "errors"}


# ---------------------------------------------------------------- the fold
def apply(state: State, event: dict) -> State:
    """Apply one event to a state in place (the step of the fold). Pure.
    The event must pass validate_event; the first schema error is the FoldError."""
    errors = validate_event(event)
    if errors:
        seq = event.get("seq") if isinstance(event, dict) else None
        raise FoldError(seq if is_int(seq) else state.last_seq + 1, errors[0])
    return apply_valid(state, event)


def apply_valid(state: State, event: dict) -> State:
    """apply() for an event the caller has already run through
    validate_event (the host's StateSync validates once to refuse it with
    the schema error): the same step without validating again."""
    seq, etype = event["seq"], event["type"]
    if seq != state.last_seq + 1:
        raise FoldError(seq, f"expected seq {state.last_seq + 1} (gapless from 1)")
    if etype != "session_start":
        if state.session_id is None:
            raise FoldError(seq, f"{etype} before session_start")
        if event["session_id"] != state.session_id:
            raise FoldError(seq, f"session_id {event['session_id']!r} != {state.session_id!r}")
    counter = _COUNTED.get(etype)
    if counter is not None:
        state.counters[counter] += 1
    else:
        _HANDLERS[etype](state, event)
    state.last_seq = seq
    return state


def _ordered(events) -> list:
    """The events sorted by seq (the list itself when already in order)."""
    if not isinstance(events, (list, tuple)):
        raise FoldError(0, f"events must be a list, got {type(events).__name__}")
    in_order = True
    prev = None
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            raise FoldError(i + 1, f"event must be a dict, got {type(e).__name__}")
        seq = e.get("seq")
        if not is_int(seq):
            raise FoldError(i + 1, f"seq must be an int, got {seq!r}")
        if prev is not None and seq < prev:
            in_order = False
        prev = seq
    if in_order:
        return events if isinstance(events, list) else list(events)
    return sorted(events, key=lambda e: e["seq"])


def _fold(ordered: list, snapshot_at, upto_seq=None, state: State | None = None
          ) -> tuple[State, dict, FoldError | None]:
    """One pass: apply every event (seq <= upto_seq, and beyond the kept
    state's last_seq when resuming), recording state_hash at each seq in
    snapshot_at. Stops at the first FoldError and returns it with the state
    as of the seq before it."""
    resume_from = state.last_seq if state is not None else 0
    state = state if state is not None else State()
    hashes: dict[int, str] = {}
    for event in ordered:
        seq = event["seq"]
        if seq <= resume_from:
            continue
        if upto_seq is not None and seq > upto_seq:
            break
        try:
            apply(state, event)
        except FoldError as exc:
            return state, hashes, exc
        if seq in snapshot_at:
            hashes[seq] = state_hash(state)
    return state, hashes, None


def fold(events, upto_seq=None, *, state: State | None = None) -> State:
    """State after the events with seq <= upto_seq (all of them when None).
    With `state`, continue from a kept State: events with seq <= state.last_seq
    are skipped and the rest applied to it in place (the host's per-turn path).
    Raises FoldError('seq N: ...') on a structural impossibility; the state is
    then left as of seq N - 1."""
    state, _, error = _fold(_ordered(events), (), upto_seq, state)
    if error is not None:
        raise error
    return state


def state_hash(state: State) -> str:
    return hash_value(state.to_canonical())


def checkpoint_body(events, prev: str | None = None, *, state: State | None = None) -> dict:
    """The body of the checkpoint that seals `events` (in seq order): through
    the last event, state_hash of their fold (`state` when the caller has
    folded them already), prev_hash = `prev`, else the chain's head as the
    events have it (the last checkpoint's state_hash, or hash_value of the
    session_start for the first). Pure."""
    if prev is None:
        last_cp = next((e for e in reversed(events) if e.get("type") == "checkpoint"), None)
        prev = last_cp["body"]["state_hash"] if last_cp else hash_value(events[0])
    return {"through_seq": events[-1]["seq"],
            "state_hash": state_hash(state if state is not None else fold(events)),
            "prev_hash": prev}


# ---------------------------------------------------------------- checks
def _format(problems: list[tuple[int, str]]) -> list[str]:
    """Unique (seq, msg) pairs, stable-sorted by seq, as 'seq N: msg'."""
    unique = list(dict.fromkeys(problems))
    unique.sort(key=lambda p: p[0])
    return [f"seq {seq}: {msg}" for seq, msg in unique]


def _verify(ordered: list, last_only: bool = False) -> tuple[State, list[tuple[int, str]]]:
    """Fold once, hashing at every checkpoint's through_seq (the last one's
    only with last_only), and check each checkpoint's state_hash and the
    prev_hash chain (first = hash_value(session_start), then the previous
    checkpoint's state_hash). Returns the folded state and the problems."""
    problems: list[tuple[int, str]] = []
    checkpoints = [e for e in ordered if e.get("type") == "checkpoint"]
    hashed = checkpoints[-1:] if last_only else checkpoints
    wanted = set()
    for cp in hashed:
        t = (cp.get("body") or {}).get("through_seq")
        if is_int(t):
            wanted.add(t)
    state, hashes, error = _fold(ordered, wanted)
    if error is not None:
        problems.append((error.seq, error.message))

    head = None
    if ordered and ordered[0].get("type") == "session_start":
        try:
            head = hash_value(ordered[0])
        except ValueError as exc:
            problems.append((1, f"session_start not hashable ({exc})"))

    prev_hash = head
    prev_through = 0
    for cp in checkpoints:
        seq = cp["seq"]
        body = cp.get("body") or {}
        t = body.get("through_seq")
        if not is_int(t, 1) or t >= seq:
            problems.append((seq, f"checkpoint.through_seq {t!r} must be in 1..{seq - 1}"))
            continue
        if t < prev_through:
            problems.append((seq, f"checkpoint.through_seq {t} < previous checkpoint's {prev_through}"))
        if prev_hash is not None and body.get("prev_hash") != prev_hash:
            problems.append((seq, f"checkpoint.prev_hash {str(body.get('prev_hash'))[:12]}... "
                                  f"does not continue the chain (expected {prev_hash[:12]}...)"))
        if cp in hashed:
            got = hashes.get(t)
            if got is None:
                problems.append((seq, f"checkpoint through_seq {t} could not be folded"))
            elif got != body.get("state_hash"):
                problems.append((seq, (
                    f"checkpoint state_hash mismatch: fold through seq {t} gives "
                    f"{got[:12]}..., log says {str(body.get('state_hash'))[:12]}...; "
                    f"the state diverged in seqs {prev_through + 1}..{t}")))
        prev_hash = body.get("state_hash") if isinstance(body.get("state_hash"), str) else None
        prev_through = t
    return state, problems


def verify_checkpoints(events, *, last_only: bool = False) -> list[str]:
    """Every checkpoint's state_hash equals the fold through its through_seq and
    the prev_hash chain holds. With last_only, only the final checkpoint's hash
    is recomputed (the chain is still checked for all). Returns 'seq N: ...'
    strings; [] when there is nothing wrong (or no checkpoint at all)."""
    try:
        ordered = _ordered(events)
    except FoldError as exc:
        return [str(exc)]
    if not any(e.get("type") == "checkpoint" for e in ordered):
        return []
    return _format(_verify(ordered, last_only)[1])


def replay(events) -> tuple[State, list[str]]:
    """validate_log + verify_checkpoints + fold, folding once. Never calls out.
    The error list is sorted by seq, so the first diverging seq comes first;
    empty == clean. The state is the fold up to the first FoldError."""
    problems = log_problems(events)
    try:
        ordered = _ordered(events)
    except FoldError as exc:
        return State(), _format(problems + [(exc.seq, exc.message)])
    state, more = _verify(ordered)
    return state, _format(problems + more)


def turn_inputs(events, turn_id: str) -> list:
    """The chat messages turn `turn_id` saw: the committed prefix as of its
    turn_start (every item with seq <= includes_through_seq, placeholders
    included). A function of the events alone."""
    ordered = _ordered(events)
    for e in ordered:
        if e.get("type") == "turn_start" and (e.get("body") or {}).get("turn_id") == turn_id:
            return fold(ordered, upto_seq=e["seq"]).committed_prefix
    raise ValueError(f"turn {turn_id!r} not found")
