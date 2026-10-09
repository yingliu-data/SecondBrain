"""events: envelope, per-type body schema, and whole-log rules."""
import copy

import pytest

from protocol import SCHEMA_VERSION
from protocol.canonical import hash_value
from protocol.events import (
    ENUMS, EVENT_TYPES, MIN_INT, OPTIONAL_BODY, REQUIRED_BODY, STR_LISTS, UNANSWERED,
    log_problems, make_event, validate_event, validate_log,
)
from tests.logbuilder import Log as _Log

SID = "sess-1"
TOOLS = [
    {"name": "bash", "side": "guest", "pin": None},
    {"name": "remember", "side": "host", "pin": None},
    {"name": "wcc_search", "side": "mcp", "pin": "ab" * 32},
]
H = "0" * 64

START = {
    "user": "u", "tenant": "t", "workflow_run_id": "run-1",
    "config_hash": H, "system_hash": H, "tools": TOOLS,
    "harness_version": "0.0.1", "image_version": "img-1", "resumed": False,
}


def turn(turn_id, includes, type="regular"):
    return {"turn_id": turn_id, "type": type, "includes_through_seq": includes,
            "model": "qwen3-14b", "params": {"temperature": 0},
            "request_hash": H, "preamble_hash": H, "workspace_tree_id": None}


def response(turn_id, content="ok"):
    return {"turn_id": turn_id, "message": {"role": "assistant", "content": content},
            "finish_reason": "stop", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def call(turn_id, call_id, name="bash", side="guest"):
    return {"turn_id": turn_id, "call_id": call_id, "name": name, "side": side,
            "args": {"cmd": "ls"}, "state": "RUNNING"}


def result(call_id, **extra):
    return {"call_id": call_id, "content": "out", **extra}


def checkpoint(through, state_hash, prev_hash):
    return {"through_seq": through, "state_hash": state_hash, "prev_hash": prev_hash}


# One minimal valid body per type (per-event tests).
VALID = {
    "session_start": START,
    "input": {"input_id": "in-1", "kind": "task", "text": "hi", "dedupe": "new"},
    "clock": {"kind": "host_hello", "at": 1_700_000_000_000},
    "turn_start": turn("turn_a", 3),
    "llm_response": response("turn_a"),
    "tool_call": call("turn_a", "c1"),
    "guard_decision": {"ref": "c1", "decision": "allow", "reason": "pass", "policy_id": "p0"},
    "op_state": {"call_id": "c1", "state": "done", "duration_ms": 5},
    "tool_result": {"call_id": "c1", "content": "out", "spill_ref": None, "rewrite": None},
    "wake": {"reason": "tool_result", "result_ids": ["c1"], "input_ids": []},
    "question": {"question_id": "q1", "call_id": "c2", "text": "?", "options": ["a", "b"],
                 "deadline": 1_700_000_300_000},
    "parked": {"checkpoint_seq": 5, "reason": "question_deadline"},
    "compaction": {"range": [2, 9], "summary_turn_id": "turn_c", "tokens_before": 10,
                   "tokens_after": 3},
    "memory": {"scope": "user", "op": "write", "slug": "likes-tea", "sha256": H,
               "derived_from": "c1"},
    "bulk": {"kind": "spill", "path": "spill/c1.txt", "sha256": H, "bytes": 123},
    "checkpoint": checkpoint(5, H, H),
    "error": {"class": "transient", "message": "502", "retry": True},
    "settle": {"input_id": "in-1", "outcome": "done", "answer": "done", "usage": {},
               "manifest": {}},
}


def ev(seq, type, body, session_id=SID):
    return make_event(session_id, seq, f"2026-10-07T00:00:{seq % 60:02d}Z", type, body)


class Log(_Log):
    """The shared builder, starting with this file's valid session_start.
    `add(type, body, **envelope)` overrides envelope keys for the broken cases."""

    def __init__(self):
        super().__init__(SID, TOOLS)
        self.start(**START)

    @property
    def start_hash(self):
        return hash_value(self.events[0])


def happy_log():
    """session_start, input, clock, turn, response, guard, call, op x2, result,
    wake, turn, response, checkpoint, settle."""
    log = Log()
    log.add("input", VALID["input"])                        # 2
    log.add("clock", VALID["clock"])                        # 3
    log.add("turn_start", turn("turn_a", 3))                # 4
    log.add("llm_response", response("turn_a"))             # 5
    log.add("guard_decision", VALID["guard_decision"])      # 6
    log.add("tool_call", call("turn_a", "c1"))              # 7
    log.add("op_state", {"call_id": "c1", "state": "running"})  # 8
    log.add("op_state", {"call_id": "c1", "state": "done", "duration_ms": 5})  # 9
    log.add("tool_result", result("c1"))                    # 10
    log.add("wake", VALID["wake"])                          # 11
    log.add("turn_start", turn("turn_b", 11))               # 12
    log.add("llm_response", response("turn_b"))             # 13
    log.add("checkpoint", checkpoint(13, "a" * 64, log.start_hash))  # 14
    log.add("settle", VALID["settle"])                      # 15
    return log


def errs(log):
    return validate_log(log.events if isinstance(log, Log) else log)


# ------------------------------------------------------------ make_event / types
def test_make_event_envelope():
    e = make_event("s", 1, "2026-10-07T00:00:00Z", "input", VALID["input"])
    assert list(e) == ["session_id", "seq", "ts", "schema", "type", "body"]
    assert e["schema"] == SCHEMA_VERSION == 1
    assert e["body"] is VALID["input"]


def test_event_types_are_the_18_of_section_5():
    assert len(EVENT_TYPES) == 18
    assert set(REQUIRED_BODY) == EVENT_TYPES
    assert set(OPTIONAL_BODY) <= EVENT_TYPES
    assert set(ENUMS) <= EVENT_TYPES
    assert set(MIN_INT) <= EVENT_TYPES and set(STR_LISTS) <= EVENT_TYPES
    assert set(VALID) == EVENT_TYPES
    assert UNANSWERED == "unanswered"


@pytest.mark.parametrize("etype", sorted(EVENT_TYPES))
def test_each_type_has_a_valid_body(etype):
    assert validate_event(ev(1, etype, VALID[etype])) == []


# ------------------------------------------------------------ envelope rules
def test_missing_envelope_key():
    e = ev(1, "input", VALID["input"])
    del e["ts"]
    assert validate_event(e) == ["ts: missing"]


def test_unknown_envelope_key():
    e = ev(1, "input", VALID["input"])
    e["extra"] = 1
    assert validate_event(e) == ["extra: unknown envelope key"]


def test_envelope_field_types():
    assert validate_event(ev(1, "input", VALID["input"], session_id="")) == [
        "session_id: expected non-empty str"]
    assert validate_event(ev(1, "input", VALID["input"], session_id=None)) == [
        "session_id: expected str, got NoneType"]
    assert validate_event(ev(0, "input", VALID["input"])) == ["seq: must be >= 1, got 0"]
    assert validate_event(ev(True, "input", VALID["input"])) == ["seq: expected int, got bool"]
    e = ev(1, "input", VALID["input"]); e["ts"] = 5
    assert validate_event(e) == ["ts: expected str, got int"]


def test_unknown_schema_version():
    e = ev(1, "input", VALID["input"]); e["schema"] = 2
    assert validate_event(e) == [f"schema: unknown version 2 (expected {SCHEMA_VERSION})"]
    e["schema"] = "1"
    assert validate_event(e) == ["schema: expected int, got str"]


def test_unknown_type():
    assert validate_event(ev(1, "bogus", {})) == ["type: unknown event type 'bogus'"]


def test_never_raises_on_junk():
    assert validate_event(None) == ["event: expected dict, got NoneType"]
    assert validate_event({}) == [f"{k}: missing" for k in
                                  ("session_id", "seq", "ts", "schema", "type", "body")]
    e = ev(1, "input", "not a dict")
    assert validate_event(e) == ["body: expected dict, got str"]
    assert validate_log(None) == ["seq 0: expected a list of events, got NoneType"]
    assert validate_log([None, 3, "x"])


# ------------------------------------------------------------ body rules
def test_missing_required_body_key():
    body = dict(VALID["input"]); del body["text"]
    assert validate_event(ev(1, "input", body)) == ["body.text: missing"]


def test_wrong_body_types_including_bool_is_not_int():
    body = dict(VALID["clock"]); body["at"] = True
    assert validate_event(ev(1, "clock", body)) == ["body.at: expected int, got bool"]
    body = dict(VALID["session_start"]); body["resumed"] = 1
    assert validate_event(ev(1, "session_start", body)) == ["body.resumed: expected bool, got int"]
    body = dict(VALID["turn_start"]); body["params"] = []
    assert "body.params: expected dict, got list" in validate_event(ev(1, "turn_start", body))


def test_extra_body_keys_are_allowed():
    body = dict(VALID["input"]); body["attachments"] = []
    assert validate_event(ev(1, "input", body)) == []


@pytest.mark.parametrize("etype,key", [
    (t, k) for t in sorted(ENUMS) for k in sorted(ENUMS[t])
])
def test_enum_values_enforced(etype, key):
    body = dict(VALID[etype]); body[key] = "nope"
    msgs = validate_event(ev(1, etype, body))
    assert msgs and msgs[0].startswith(f"body.{key}: 'nope' not in")


def test_tool_call_state_must_be_running():
    body = dict(VALID["tool_call"]); body["state"] = "done"
    assert validate_event(ev(1, "tool_call", body)) == ["body.state: 'done' not in ['RUNNING']"]


def test_clock_kind_fixed_set_plus_grace_prefix():
    for kind in ("host_hello", "host_heartbeat", "turn_clock", "grace_tool",
                 "grace_park", "idle_timer", "question_deadline", "wall_clock",
                 "resumed"):
        assert validate_event(ev(1, "clock", {"kind": kind, "at": 1})) == []
    msgs = validate_event(ev(1, "clock", {"kind": "lunch", "at": 1}))
    assert len(msgs) == 1 and msgs[0].startswith("body.kind: 'lunch' not in") and msgs[0].endswith("or grace_*")


def test_session_start_tools_entries_validated():
    def with_tools(tools):
        body = dict(START); body["tools"] = tools
        return validate_event(ev(1, "session_start", body))
    assert with_tools([]) == []
    assert with_tools(["bash"]) == ["body.tools[0]: expected dict, got str"]
    assert any("duplicate" in m for m in with_tools([TOOLS[0], TOOLS[0]]))
    assert any("side" in m for m in with_tools([{"name": "x", "side": "cloud", "pin": None}]))
    assert any("pin" in m for m in with_tools([{"name": "x", "side": "host"}]))
    assert any("pin" in m for m in with_tools([{"name": "x", "side": "host", "pin": 1}]))
    assert any("name" in m for m in with_tools([{"name": "", "side": "host", "pin": None}]))


def test_compaction_range_shape():
    def rng(r):
        body = dict(VALID["compaction"]); body["range"] = r
        return validate_event(ev(1, "compaction", body))
    assert rng([2, 2]) == []
    assert rng([2]) and rng([2, "x"]) and rng([5, 2]) and rng([0, 2])


def test_list_of_str_fields():
    body = dict(VALID["wake"]); body["result_ids"] = [1]
    assert validate_event(ev(1, "wake", body)) == ["body.result_ids: expected list of str"]
    body = dict(VALID["question"]); body["options"] = [None]
    assert validate_event(ev(1, "question", body)) == ["body.options: expected list of str"]


def test_optional_keys_are_typed_when_present():
    assert validate_event(ev(1, "tool_result", result("c1", late=True))) == []
    assert validate_event(ev(1, "tool_result", result("c1", late="yes"))) == [
        "body.late: expected bool, got str"]
    assert validate_event(ev(1, "op_state", {"call_id": "c1", "state": "queued"})) == []
    assert validate_event(ev(1, "op_state", {"call_id": "c1", "state": "queued",
                                             "duration_ms": -1}))
    body = dict(VALID["llm_response"]); body["response_id"] = "chatcmpl-1"
    assert validate_event(ev(1, "llm_response", body)) == []


@pytest.mark.parametrize("etype,key", [
    (t, k) for t in sorted(MIN_INT) for k in sorted(MIN_INT[t])
])
def test_int_lower_bounds_enforced(etype, key):
    minimum = MIN_INT[etype][key]
    body = dict(VALID[etype]); body[key] = minimum - 1
    assert validate_event(ev(1, etype, body)) == [f"body.{key}: must be >= {minimum}"]
    body[key] = minimum
    assert validate_event(ev(1, etype, body)) == []
    body[key] = "x"                                             # the type error, once
    assert validate_event(ev(1, etype, body)) == [f"body.{key}: expected int, got str"]


def test_negative_counters_rejected():
    body = dict(VALID["bulk"]); body["bytes"] = -1
    assert validate_event(ev(1, "bulk", body)) == ["body.bytes: must be >= 0"]
    body = dict(VALID["checkpoint"]); body["through_seq"] = 0
    assert validate_event(ev(1, "checkpoint", body)) == ["body.through_seq: must be >= 1"]


def test_body_finite_floats_are_fine():
    body = dict(VALID["tool_call"]); body["args"] = {"x": 1.5}
    assert validate_event(ev(1, "tool_call", body)) == []


def test_body_must_be_canonicalisable_no_nan():
    body = dict(VALID["tool_call"]); body["args"] = {"x": float("nan")}
    msgs = validate_event(ev(1, "tool_call", body))
    assert msgs == ["body: not canonicalisable (body.args.x: non-finite float nan)"]
    body["args"] = {1: "x"}
    assert validate_event(ev(1, "tool_call", body)) == [
        "body: not canonicalisable (body.args: non-string key 1)"]


# ------------------------------------------------------------ log rules
def test_happy_log_is_valid():
    assert errs(happy_log()) == []


def test_empty_log():
    assert validate_log([]) == ["seq 0: empty log (first event must be session_start)"]


def test_first_event_must_be_session_start():
    log = [ev(1, "input", VALID["input"])]
    assert validate_log(log) == ["seq 1: first event must be session_start, got 'input'"]


def test_only_one_session_start():
    log = Log(); log.add("session_start", START)
    assert errs(log) == ["seq 2: second session_start (only one allowed)"]


def test_seq_gapless_from_one():
    log = happy_log()
    log.events[3]["seq"] = 40
    assert errs(log) == ["seq 40: expected seq 4 (gapless, increasing from 1)"]
    log = happy_log()
    log.events[0]["seq"] = 2
    assert "seq 2: expected seq 1 (gapless, increasing from 1)" in errs(log)
    log = happy_log()
    log.events[2]["seq"] = 2  # duplicate
    assert "seq 2: expected seq 3 (gapless, increasing from 1)" in errs(log)
    log = happy_log()
    del log.events[3]["seq"]  # labelled by position
    assert errs(log) == ["seq 4: seq: missing"]


def test_one_session_id():
    log = happy_log()
    log.events[5]["session_id"] = "other"
    assert errs(log) == ["seq 6: session_id 'other' != 'sess-1'"]
    log = Log()
    log.add("input", VALID["input"], session_id="other")       # the builder's envelope override
    assert errs(log) == ["seq 2: session_id 'other' != 'sess-1'"]


def test_per_event_errors_are_prefixed_and_cross_checks_skipped():
    log = happy_log()
    log.events[9]["body"] = {"call_id": "ghost"}   # tool_result missing content
    msgs = errs(log)
    assert msgs == ["seq 10: body.content: missing"]


def test_unknown_type_in_log():
    log = Log(); log.add("bogus", {})
    assert errs(log) == ["seq 2: type: unknown event type 'bogus'"]


def test_log_problems_is_validate_log_unformatted():
    log = happy_log()
    log.events[5]["session_id"] = "other"
    assert log_problems(log.events) == [(6, "session_id 'other' != 'sess-1'")]
    assert validate_log(log.events) == [f"seq {s}: {m}" for s, m in log_problems(log.events)]


def test_state_dependent_rules_are_not_log_rules():
    """References that need the folded state (turns, calls, offered tools,
    checkpoint chain) are the fold's: validate_log passes them through."""
    log = Log()
    log.add("turn_start", turn("turn_a", 1))
    log.add("llm_response", response("turn_zz"))                       # unknown turn
    log.add("tool_call", call("turn_a", "c1", name="forget", side="host"))   # not offered
    log.add("tool_result", result("c1"))
    log.add("tool_result", result("c1"))                               # second, not flagged late
    log.add("checkpoint", checkpoint(5, "b" * 64, "c" * 64))           # chain unchecked here
    assert errs(log) == []


def compaction_log():
    log = happy_log()                                               # ..15
    log.add("turn_start", turn("turn_c", 15, type="compaction"))     # 16
    log.add("llm_response", response("turn_c", "summary"))          # 17
    log.add("compaction", {"range": [2, 15], "summary_turn_id": "turn_c",
                           "tokens_before": 100, "tokens_after": 10})   # 18
    return log


def test_compaction_happy():
    assert errs(compaction_log()) == []


def test_memory_derived_from_call_or_compaction():
    log = compaction_log()
    log.add("memory", {**VALID["memory"], "derived_from": "c1"})
    log.add("memory", {**VALID["memory"], "derived_from": "turn_c"})
    log.add("memory", {**VALID["memory"], "derived_from": "turn_a"})
    assert errs(log) == [
        "seq 21: memory.derived_from 'turn_a' names no tool_call call_id or compaction summary_turn_id"]


def test_checkpoint_through_seq_below_own_seq():
    log = happy_log()
    log.add("checkpoint", checkpoint(16, "b" * 64, "a" * 64))
    assert errs(log) == ["seq 16: checkpoint.through_seq 16 must be < seq 16"]


def test_settle_references_an_input():
    log = Log(); log.add("settle", VALID["settle"])
    assert errs(log) == ["seq 2: settle.input_id 'in-1' names no input"]


def test_validate_log_is_pure():
    log = happy_log()
    before = copy.deepcopy(log.events)
    assert validate_log(log.events) == validate_log(log.events)
    assert log.events == before


def test_op_state_input_required_carries_its_request():
    request = {"request_state": "rs-1", "schema": {"type": "object"}}
    ok = {"call_id": "c1", "state": "input_required", "attempt": 0, "input_request": request}
    assert validate_event(ev(1, "op_state", ok)) == []
    assert validate_event(ev(1, "op_state", {"call_id": "c1", "state": "running", "attempt": 1})) == []
    assert validate_event(ev(1, "op_state", {"call_id": "c1", "state": "input_required"})) == [
        "body.input_request: missing (state 'input_required')"]
    assert validate_event(ev(1, "op_state", {**ok, "state": "running"})) == [
        "body.input_request: only with state 'input_required'"]
    bad = {**ok, "input_request": {"request_state": "", "schema": None}}
    assert validate_event(ev(1, "op_state", bad)) == [
        "body.input_request.request_state: expected non-empty str",
        "body.input_request.schema: expected dict"]
