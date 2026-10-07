"""fold, checkpoint chain, replay and the goldens (PLAN.md sections 5, 12, 14)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from protocol.canonical import canonical_json, hash_value, sha256_hex
from protocol.events import validate_log
from protocol.fold import (
    PLACEHOLDER, FoldError, State, apply, fold, replay, state_hash, turn_inputs,
    verify_checkpoints,
)
from tests.golden import make_goldens
from tests.logbuilder import Log

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDENS = list(make_goldens.GOLDENS)


def golden(name: str) -> list[dict]:
    with open(GOLDEN_DIR / f"{name}.jsonl", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------- goldens
@pytest.mark.parametrize("name", GOLDENS)
def test_golden_validates(name):
    assert validate_log(golden(name)) == []


@pytest.mark.parametrize("name", GOLDENS)
def test_golden_folds(name):
    state = fold(golden(name))
    assert state.last_settle["outcome"] == "done"
    assert state.committed_prefix[0]["role"] == "user"
    assert state.committed_prefix[0]["preamble"] is not None


@pytest.mark.parametrize("name", GOLDENS)
def test_golden_replays_clean(name):
    state, errors = replay(golden(name))
    assert errors == []
    assert state.counters["checkpoints"] == 1


@pytest.mark.parametrize("name", GOLDENS)
def test_golden_generator_is_deterministic(name):
    frozen = (GOLDEN_DIR / f"{name}.jsonl").read_bytes()
    fresh = make_goldens.render(make_goldens.GOLDENS[name]()).encode("utf-8")
    assert frozen == fresh, f"{name}.jsonl drifted from make_goldens.py"


def test_golden_seqs_gapless_and_ts_fixed():
    for name in GOLDENS:
        events = golden(name)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert all(isinstance(e["ts"], str) for e in events)
        assert events[-1]["type"] == "checkpoint"


# ---------------------------------------------------------------- replay detects corruption
def test_corrupted_event_reports_first_diverging_seq():
    events = golden("text_answer")
    events[4]["body"]["message"]["content"] = "2 + 2 = 5."     # seq 5, the llm_response
    _, errors = replay(events)
    assert errors, "corruption must be reported"
    assert errors[0].startswith("seq 7:")                       # the checkpoint that caught it
    assert "state_hash mismatch" in errors[0]
    assert "1..6" in errors[0]                                  # the window the divergence is in


def test_corruption_after_a_verified_checkpoint_narrows_the_window():
    log = Log()
    log.start()
    log.input("i1", "hi")
    log.turn("t1")
    log.response("t1", "hello")
    log.settle("i1")
    log.checkpoint()                                            # seq 6, through 5
    log.input("i2", "again")
    log.turn("t2")
    log.response("t2", "hello again")
    log.settle("i2")
    log.checkpoint()                                            # seq 11, through 10
    events = log.events
    assert replay(events)[1] == []
    events[8]["body"]["message"]["content"] = "tampered"         # seq 9
    _, errors = replay(events)
    assert errors[0].startswith("seq 11:")
    assert "6..10" in errors[0]


def test_broken_prev_hash_is_an_error():
    events = golden("mcp_tool_via_gateway")
    events[-1]["body"]["prev_hash"] = "0" * 64
    _, errors = replay(events)
    assert any("prev_hash" in e and e.startswith(f"seq {events[-1]['seq']}:") for e in errors)
    assert verify_checkpoints(events)


def test_first_checkpoint_prev_hash_is_hash_of_session_start():
    log = Log()
    log.start()
    log.input("i1", "hi")
    log.checkpoint()
    assert log.events[-1]["body"]["prev_hash"] == hash_value(log.events[0])
    assert verify_checkpoints(log.events) == []
    log.events[-1]["body"]["prev_hash"] = "b" * 64
    errs = verify_checkpoints(log.events)
    assert len(errs) == 1 and errs[0].startswith("seq 3:") and "chain" in errs[0]


def test_chain_links_second_checkpoint_to_first():
    log = Log()
    log.start()
    log.input("i1", "hi")
    log.turn("t1")
    log.response("t1", "a")
    log.checkpoint()
    log.settle("i1")
    log.checkpoint()
    assert verify_checkpoints(log.events) == []
    log.events[-1]["body"]["prev_hash"] = hash_value(log.events[0])   # wrong link
    errs = verify_checkpoints(log.events)
    assert len(errs) == 1 and errs[0].startswith("seq 7:") and "chain" in errs[0]


def test_checkpoint_through_seq_bounds_and_monotone():
    log = Log()
    log.start()
    log.input("i1", "hi")
    log.checkpoint()                                            # seq 3, through 2
    log.turn("t1")
    log.checkpoint()                                            # seq 5, through 4
    log.events[-1]["body"]["through_seq"] = 1                   # goes backwards
    errs = verify_checkpoints(log.events)
    assert any(e.startswith("seq 5:") and "< previous checkpoint's 2" in e for e in errs)
    log.events[-1]["body"]["through_seq"] = 5                   # not below its own seq
    errs = verify_checkpoints(log.events)
    assert errs == ["seq 5: checkpoint.through_seq 5 must be in 1..4"]


def test_verify_last_only_hashes_only_the_final_checkpoint():
    log = Log()
    log.start()
    log.input("i1", "hi")
    log.turn("t1")
    log.response("t1", "a")
    log.checkpoint()                                            # seq 5, through 4
    log.settle("i1")
    log.checkpoint()                                            # seq 7, through 6
    log.events[4]["body"]["state_hash"] = "0" * 64              # first checkpoint lies
    log.events[6]["body"]["prev_hash"] = "0" * 64               # ...and the chain follows it
    full = verify_checkpoints(log.events)
    assert [e.split(":")[0] for e in full] == ["seq 5"]         # only the first is wrong
    assert verify_checkpoints(log.events, last_only=True) == []
    log.events[6]["body"]["state_hash"] = "1" * 64
    last = verify_checkpoints(log.events, last_only=True)
    assert len(last) == 1 and last[0].startswith("seq 7:") and "mismatch" in last[0]
    log.events[6]["body"]["prev_hash"] = "bad"                  # the chain is still checked
    assert any("chain" in e for e in verify_checkpoints(log.events, last_only=True))


def test_replay_errors_sorted_by_seq():
    events = golden("device_tool_late_duplicate")
    events[-1]["body"]["prev_hash"] = "bad"
    events[4]["body"]["message"]["content"] = "tampered"
    events[2]["body"]["at"] = "not-an-int"                      # seq 3 schema error
    _, errors = replay(events)
    seqs = [int(e.split()[1].rstrip(":")) for e in errors]
    assert seqs == sorted(seqs)
    assert seqs[0] == 3
    assert len([e for e in errors if e.startswith("seq 3:")]) == 1   # reported once


def test_replay_handles_junk_without_raising():
    state, errors = replay(None)
    assert state.last_seq == 0 and errors and errors[0].startswith("seq 0:")
    state, errors = replay([{"seq": "x"}])
    assert state.last_seq == 0 and errors


# ---------------------------------------------------------------- invariants
def _two_device_calls() -> Log:
    log = Log()
    log.start()
    log.input("i1", "calendar and reminders please")
    log.clock(1000)
    log.turn("t1")
    log.response("t1", calls=[("cA", "calendar", {}), ("cB", "reminders", {})])
    log.call("t1", "cA", "calendar", "device")
    log.call("t1", "cB", "reminders", "device")
    return log


def test_invariant5_swapped_result_order_folds_to_same_prefix():
    a = _two_device_calls()
    a.result("cA", "cal-result")
    a.result("cB", "rem-result")
    a.turn("t2")
    b = _two_device_calls()
    b.result("cB", "rem-result")
    b.result("cA", "cal-result")
    b.turn("t2")
    pa, pb = fold(a.events).committed_prefix, fold(b.events).committed_prefix
    assert canonical_json(pa) == canonical_json(pb)
    tool_ids = [m["tool_call_id"] for m in pa if m["role"] == "tool"]
    assert tool_ids == ["cA", "cB"]                              # call order, not arrival order
    assert fold(a.events).prefix_origins == fold(b.events).prefix_origins


def test_duplicate_input_folds_once():
    state = fold(golden("device_tool_late_duplicate"))
    users = [m for m in state.committed_prefix if m["role"] == "user"]
    assert len(users) == 1
    assert state.seen_input_ids == {"in-0002"}
    assert state.counters["duplicates"] == 1
    assert state.staged_suffix[-1]["message"]["role"] == "assistant"


def test_new_input_with_a_seen_id_is_a_fold_error():
    log = Log()
    log.start()
    log.input("i1", "x")
    log.input("i1", "x again", kind="follow_up")
    with pytest.raises(FoldError, match=r"^seq 3: input_id 'i1' seen before"):
        fold(log.events)
    log.events[-1]["body"]["dedupe"] = "late"                   # recorded, not acted on
    state = fold(log.events)
    assert state.counters["late_inputs"] == 1 and len(state.staged_suffix) == 1


def test_late_result_for_terminal_call_is_recorded_and_ignored():
    events = golden("device_tool_late_duplicate")
    before = fold(events, upto_seq=16)
    after = fold(events, upto_seq=17)
    assert events[16]["type"] == "tool_result" and events[16]["body"]["late"] is True
    assert canonical_json(before.committed_prefix) == canonical_json(after.committed_prefix)
    assert canonical_json(before.staged_suffix) == canonical_json(after.staged_suffix)
    assert after.counters["late_results"] == before.counters["late_results"] + 1


def _async_placeholder_log() -> Log:
    log = Log()
    log.start()
    log.input("i1", "do it")
    log.turn("t1")
    log.response("t1", calls=[("cA", "calendar", {})])
    log.call("t1", "cA", "calendar", "device")
    log.input("i2", "meanwhile...", kind="follow_up")
    log.turn("t2")                                              # async: the call is still open
    log.response("t2", "waiting on the calendar")
    return log


def test_late_result_after_committed_placeholder_never_touches_prefix():
    log = _async_placeholder_log()
    before = fold(log.events)
    assert "cA" in before.placeholders
    tool_msgs = [m for m in before.committed_prefix if m["role"] == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_call_id": "cA", "content": PLACEHOLDER}]
    prefix_before = canonical_json(before.committed_prefix)

    log.result("cA", "real result", late=True)
    after = fold(log.events)
    assert validate_log(log.events) == []                       # the log rules agree with the fold
    assert canonical_json(after.committed_prefix) == prefix_before
    new_items = [it for it in after.staged_suffix if it["message"].get("tool_call_id") == "cA"]
    assert len(new_items) == 1
    assert new_items[0]["message"]["content"] == "real result"
    assert new_items[0]["origin"] == log.events[-1]["seq"]      # a NEW item, at the end
    assert after.counters["placeholder_fills"] == 1
    assert after.open_calls["cA"]["result"] == log.events[-1]["seq"]

    log.turn("t3")
    final = fold(log.events)
    assert canonical_json(final.committed_prefix[:len(before.committed_prefix)]) == prefix_before
    assert final.committed_prefix[-1]["content"] == "real result"


def test_the_one_late_rule():
    """late == (the call already has a result) or (its placeholder was committed),
    and body.late must say so."""
    # a first result after a committed placeholder must be flagged late
    log = _async_placeholder_log()
    log.result("cA", "real result")
    with pytest.raises(FoldError, match=r"^seq 9: .*committed placeholder but late is False"):
        fold(log.events)
    # a late flag on a result that is neither is a fold error
    log = Log()
    log.start()
    log.input("i1", "x")
    log.turn("t1")
    log.response("t1", calls=[("c1", "calendar", {})])
    log.call("t1", "c1", "calendar", "device")
    log.result("c1", "one", late=True)
    with pytest.raises(FoldError, match=r"^seq 6: .*no earlier result or committed placeholder"):
        fold(log.events)
    # a second result needs the flag, and is then counted and dropped
    log.events[-1]["body"].pop("late")
    log.result("c1", "two")
    with pytest.raises(FoldError, match=r"^seq 7: .*already has a result but late is False"):
        fold(log.events)
    log.events[-1]["body"]["late"] = True
    state = fold(log.events)
    assert state.counters["late_results"] == 1 and state.open_calls["c1"]["result"] == 6
    assert [it["message"]["content"] for it in state.staged_suffix if it["message"]["role"] == "tool"] == ["one"]
    assert validate_log(log.events) == []


def test_fold_never_reads_ts():
    for name in GOLDENS:
        events = golden(name)
        h1 = state_hash(fold(events))
        for e in events:
            e["ts"] = "x"
        assert state_hash(fold(events)) == h1


def test_fold_is_pure(monkeypatch):
    import os as _os
    import random
    import time

    def boom(*a, **k):
        raise AssertionError("fold touched an impure source")

    monkeypatch.setattr(time, "time", boom)
    monkeypatch.setattr(time, "monotonic", boom)
    monkeypatch.setattr(time, "time_ns", boom)
    monkeypatch.setattr(_os, "urandom", boom)
    monkeypatch.setattr(random, "random", boom)
    monkeypatch.setattr(_os, "getenv", boom)
    monkeypatch.setattr(_os.environ, "get", boom)
    for name in GOLDENS:
        events = golden(name)
        state, errors = replay(events)
        assert errors == []
        assert state_hash(state)
        turn_inputs(events, next(iter(state.turns)))


def test_upto_seq_truncates():
    events = golden("text_answer")
    s2 = fold(events, upto_seq=2)
    assert s2.last_seq == 2
    assert s2.committed_prefix == [] and len(s2.staged_suffix) == 1
    assert s2.turns == {} and s2.clock_at is None
    s4 = fold(events, upto_seq=4)
    assert len(s4.committed_prefix) == 1 and s4.staged_suffix == []
    assert s4.clock_at == events[2]["body"]["at"]
    assert state_hash(fold(events, upto_seq=99)) == state_hash(fold(events))
    assert state_hash(fold(events, upto_seq=0)) == state_hash(State())


@pytest.mark.parametrize("name", GOLDENS)
def test_fold_resumes_from_a_kept_state(name):
    events = golden(name)
    half = len(events) // 2
    kept = fold(events, upto_seq=half)
    resumed = fold(events, state=kept)                          # the full log, half skipped
    assert resumed is kept and resumed.last_seq == len(events)
    assert state_hash(resumed) == state_hash(fold(events))
    tail_only = fold(events[half:], state=fold(events, upto_seq=half))   # just the tail
    assert state_hash(tail_only) == state_hash(fold(events))
    # the next event must continue the kept state
    with pytest.raises(FoldError, match=rf"^seq {half + 2}: expected seq {half + 1}"):
        fold(events[half + 1:], state=fold(events, upto_seq=half))


def test_fold_resume_one_event_at_a_time_matches_apply():
    events = golden("device_tool_late_duplicate")
    state = State()
    for e in events:
        fold([e], state=state)
    assert state_hash(state) == state_hash(fold(events))
    step = State()
    for e in events:
        apply(step, e)
    assert state_hash(step) == state_hash(state)


def test_apply_rejects_a_malformed_event_with_its_schema_error():
    events = golden("text_answer")
    state = fold(events, upto_seq=2)
    bad = dict(events[2]); bad["body"] = {"kind": "turn_clock", "at": "noon"}
    with pytest.raises(FoldError, match=r"^seq 3: body.at: expected int, got str"):
        apply(state, bad)
    assert state.last_seq == 2
    with pytest.raises(FoldError, match=r"^seq 3: event: expected dict"):
        apply(state, "junk")


def test_compaction_rebuilds_prefix_from_summary():
    log = Log()
    log.start()
    log.input("i1", "first")                                    # 2
    log.turn("t1")                                              # 3
    log.response("t1", "answer one")                            # 4
    log.input("i2", "second", kind="follow_up")                 # 5
    log.turn("t2")                                              # 6
    log.response("t2", "answer two")                            # 7
    log.turn("t3", type="compaction")                           # 8: the model summarises
    summary = {"role": "assistant", "content": "SUMMARY of seqs 2..7"}
    log.add("llm_response", {"turn_id": "t3", "message": summary, "finish_reason": "stop",
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}})  # 9
    log.add("compaction", {"range": [2, 7], "summary_turn_id": "t3",
                           "tokens_before": 900, "tokens_after": 90})               # 10
    log.input("i3", "third", kind="follow_up")                  # 11
    log.turn("t4")                                              # 12
    state = fold(log.events)
    assert state.compacted_through == 7
    roles = [(m["role"], m["content"]) for m in state.committed_prefix]
    assert roles == [("assistant", "SUMMARY of seqs 2..7"), ("user", "third")]
    assert state.prefix_origins == [2, 11]
    assert all(it["seq"] != 9 for it in state.staged_suffix)
    assert validate_log(log.events) == []
    # the turn that ran after compaction saw the summary, the earlier turn saw the raw messages
    assert [m["content"] for m in turn_inputs(log.events, "t4")][0] == "SUMMARY of seqs 2..7"
    assert [m["content"] for m in turn_inputs(log.events, "t2")] == ["first", "answer one", "second"]


def test_compaction_keeps_items_outside_the_range():
    log = Log()
    log.start()
    log.input("i1", "keep me")                                  # 2
    log.turn("t1")                                              # 3
    log.response("t1", "drop me")                               # 4
    log.input("i2", "drop me too", kind="follow_up")            # 5
    log.turn("t2")                                              # 6
    log.response("t2", "drop me three")                         # 7
    log.turn("t3", type="compaction")                           # 8
    log.add("llm_response", {"turn_id": "t3", "message": {"role": "assistant", "content": "S"},
                             "finish_reason": "stop", "usage": {}})                  # 9
    log.add("compaction", {"range": [3, 7], "summary_turn_id": "t3",
                           "tokens_before": 9, "tokens_after": 1})                   # 10
    state = fold(log.events)
    assert [m["content"] for m in state.committed_prefix] == ["keep me", "S"]


def _compaction_log(summary_type="compaction", range_to=7, skip_response=False) -> Log:
    log = Log()
    log.start()
    log.input("i1", "first")                                    # 2
    log.turn("t1")                                              # 3
    log.response("t1", "answer one")                            # 4
    log.input("i2", "second", kind="follow_up")                 # 5
    log.turn("t2")                                              # 6
    log.response("t2", "answer two")                            # 7
    log.turn("t3", type=summary_type)                           # 8
    if not skip_response:
        log.response("t3", "S")                                 # 9
    log.add("compaction", {"range": [2, range_to], "summary_turn_id": "t3",
                           "tokens_before": 9, "tokens_after": 1})
    return log


def test_compaction_reference_rules_are_fold_errors():
    with pytest.raises(FoldError, match=r"^seq 9: .*has no llm_response"):
        fold(_compaction_log(skip_response=True).events)
    with pytest.raises(FoldError, match=r"^seq 10: .*'regular' turn, not a compaction turn"):
        fold(_compaction_log(summary_type="regular").events)
    with pytest.raises(FoldError, match=r"^seq 10: compaction.range \[2, 8\] must cover only seqs below"):
        fold(_compaction_log(range_to=8).events)
    log = _compaction_log()
    log.events[-1]["body"]["summary_turn_id"] = "ghost"
    with pytest.raises(FoldError, match=r"^seq 10: .*'ghost' unknown"):
        fold(log.events)


# ---------------------------------------------------------------- fold errors
def test_turn_start_before_session_start():
    log = Log()
    log.turn("t1")
    with pytest.raises(FoldError, match=r"^seq 1: turn_start before session_start"):
        fold(log.events)


def test_second_session_start_is_a_fold_error():
    log = Log()
    log.start()
    log.start()
    with pytest.raises(FoldError, match=r"^seq 2: session_start must be the first event"):
        fold(log.events)


def test_wrong_session_id_is_a_fold_error():
    log = Log()
    log.start()
    log.input("i1", "x")
    log.events[-1]["session_id"] = "other"
    with pytest.raises(FoldError, match=r"^seq 2: session_id 'other' != 'sess-test'"):
        fold(log.events)


def test_tool_result_for_unknown_call():
    log = Log()
    log.start()
    log.input("i1", "x")
    log.turn("t1")
    log.result("nope", "?")
    with pytest.raises(FoldError, match=r"^seq 4: .*unknown call 'nope'"):
        fold(log.events)
    log.events.pop()                                            # op_state shares the rule
    log.add("op_state", {"call_id": "nope", "state": "lost"})
    with pytest.raises(FoldError, match=r"^seq 4: op_state for unknown call 'nope'"):
        fold(log.events)


def test_tool_call_not_offered_is_rejected():
    log = Log()
    log.start()
    log.input("i1", "x")
    log.turn("t1")
    log.response("t1", calls=[("c1", "calendar", {})])
    log.call("t1", "c1", "calendar", "host")                    # wrong side
    with pytest.raises(FoldError, match=r"^seq 5: .*not offered"):
        fold(log.events)


def test_turn_and_call_reference_rules_are_fold_errors():
    log = Log()
    log.start()
    log.input("i1", "x")
    log.turn("t1")
    log.response("t1", calls=[("c1", "bash", {})])
    log.call("t1", "c1", "bash", "guest")
    good = len(log.events)
    log.call("t1", "c1", "bash", "guest")
    with pytest.raises(FoldError, match=r"^seq 6: duplicate call_id 'c1'"):
        fold(log.events)
    del log.events[good:]
    log.call("tX", "c2", "bash", "guest")
    with pytest.raises(FoldError, match=r"^seq 6: tool_call for unknown turn 'tX'"):
        fold(log.events)
    del log.events[good:]
    log.response("t1", "again")
    with pytest.raises(FoldError, match=r"^seq 6: second llm_response for turn 't1'"):
        fold(log.events)
    del log.events[good:]
    log.response("tX", "?")
    with pytest.raises(FoldError, match=r"^seq 6: llm_response for unknown turn 'tX'"):
        fold(log.events)
    del log.events[good:]
    log.turn("t1")
    with pytest.raises(FoldError, match=r"^seq 6: duplicate turn_id 't1'"):
        fold(log.events)
    del log.events[good:]
    log.turn("t2", includes=2)                                   # t1 included through 2 as well
    fold(log.events)
    log.events[-1]["body"]["includes_through_seq"] = 1
    with pytest.raises(FoldError, match=r"^seq 6: includes_through_seq goes backwards"):
        fold(log.events)
    log.events[-1]["body"]["includes_through_seq"] = 6
    with pytest.raises(FoldError, match=r"^seq 6: includes_through_seq 6 must be in 0..5"):
        fold(log.events)


def test_parked_references_an_earlier_checkpoint():
    log = Log()
    log.start()
    log.input("i1", "x")
    log.checkpoint()                                            # seq 3
    log.add("parked", {"checkpoint_seq": 2, "reason": "question_deadline"})
    with pytest.raises(FoldError, match=r"^seq 4: parked.checkpoint_seq 2 is not the seq"):
        fold(log.events)
    log.events[-1]["body"]["checkpoint_seq"] = 3
    assert fold(log.events).parked_at == 3


def test_seq_gap_is_a_fold_error():
    events = golden("text_answer")
    del events[2]
    with pytest.raises(FoldError, match=r"^seq 4: expected seq 3"):
        fold(events)


def test_unsorted_events_are_folded_in_seq_order():
    events = golden("text_answer")
    shuffled = events[::-1]
    assert state_hash(fold(shuffled)) == state_hash(fold(events))
    assert shuffled[0]["seq"] == events[-1]["seq"]              # the input list is untouched


def test_replay_returns_partial_state_on_fold_error():
    events = golden("text_answer")
    del events[2]
    state, errors = replay(events)
    assert errors and any(e.startswith("seq 4: expected seq 3") for e in errors)
    assert state.last_seq == 2


# ---------------------------------------------------------------- per-type rules
def test_steer_is_staged_like_a_task_and_committed_at_next_turn():
    log = Log()
    log.start()
    log.input("i1", "task")
    log.turn("t1")
    log.response("t1", calls=[("c1", "calendar", {})])
    log.call("t1", "c1", "calendar", "device")
    log.input("s1", "actually, skip weekends", kind="steer")    # 6
    mid = fold(log.events)
    assert mid.queues["steer"] == [6]
    staged = [it for it in mid.staged_suffix if it["message"]["role"] == "user"]
    assert staged == [{"seq": 6, "origin": 6, "message": {
        "role": "user", "content": "actually, skip weekends", "input_id": "s1",
        "kind": "steer", "preamble": None}}]
    assert all(m["content"] != "actually, skip weekends" for m in mid.committed_prefix)
    log.result("c1", "r")                                       # 7, origin 5
    log.turn("t2")
    end = fold(log.events)
    assert end.queues["steer"] == [] and end.staged_suffix == []
    steer = [m for m in end.committed_prefix if m.get("kind") == "steer"]
    assert steer == [{"role": "user", "content": "actually, skip weekends", "input_id": "s1",
                      "kind": "steer", "preamble": "p1"}]       # first user message turn 2 committed
    assert end.committed_prefix.index(steer[0]) > [m["role"] for m in end.committed_prefix].index("tool")
    assert end.prefix_origins == [2, 4, 5, 6]


def test_steer_not_yet_included_waits_for_the_next_turn():
    log = Log()
    log.start()
    log.input("i1", "task")                                     # 2
    log.input("s1", "faster", kind="steer")                     # 3
    log.turn("t1", includes=2)                                  # 4: built before the steer
    state = fold(log.events)
    assert [m["content"] for m in state.committed_prefix] == ["task"]
    assert state.queues["steer"] == [3]
    log.turn("t2")
    assert [m["content"] for m in fold(log.events).committed_prefix] == ["task", "faster"]


def test_follow_up_queue_drains_at_turn_start():
    log = Log()
    log.start()
    log.input("i1", "task")
    log.turn("t1")
    log.response("t1", "a")
    log.input("i2", "more", kind="follow_up")
    assert fold(log.events).queues["follow_up"] == [5]
    log.turn("t2")
    assert fold(log.events).queues["follow_up"] == []


def test_question_opened_and_answered():
    log = Log()
    log.start()
    log.input("i1", "task")
    log.turn("t1")
    log.response("t1", calls=[("q1", "question", {"text": "which?"})])
    log.call("t1", "q1", "question", "guest")
    log.add("question", {"question_id": "question_x", "call_id": "q1", "text": "which?",
                         "options": ["a", "b"], "deadline": 5000})
    opened = fold(log.events)
    assert opened.open_questions["question_x"] == {"deadline": 5000, "call_id": "q1",
                                                   "answered": False, "seq": 6}
    log.input("u1", "a", kind="ui_response", question_id="question_x", answer="a")
    answered = fold(log.events)
    q = answered.open_questions["question_x"]
    assert q["answered"] is True and q["answer"] == "a" and q["answered_seq"] == 7
    assert all(m["role"] != "user" or m["content"] != "a" for m in answered.committed_prefix)
    assert answered.staged_suffix[-1]["message"]["role"] != "user"   # not a chat message
    log.input("u2", "b", kind="ui_response", question_id="question_x", answer="b")
    with pytest.raises(FoldError, match=r"^seq 8: question 'question_x' answered twice"):
        fold(log.events)


def test_ui_response_for_unknown_question_is_a_fold_error():
    log = Log()
    log.start()
    log.input("u1", "a", kind="ui_response", question_id="ghost")
    with pytest.raises(FoldError, match=r"^seq 2: .*unknown question"):
        fold(log.events)
    log.events[-1]["body"].pop("question_id")
    with pytest.raises(FoldError, match=r"^seq 2: ui_response without question_id"):
        fold(log.events)


def test_guard_deny_unanswered_counts_and_park_settle():
    log = Log()
    log.start()
    log.input("i1", "task")
    log.turn("t1")
    log.response("t1", "a")
    log.add("guard_decision", {"ref": "question_x", "decision": "deny",
                               "reason": "unanswered", "policy_id": "question_timeout"})
    log.add("guard_decision", {"ref": "c9", "decision": "deny",
                               "reason": "allowlist", "policy_id": "allowlist"})
    log.checkpoint()
    log.add("parked", {"checkpoint_seq": 7, "reason": "question_deadline"})
    mid = fold(log.events)
    assert mid.denied_questions == 1
    assert mid.counters["guard_deny"] == 2
    assert mid.parked is True and mid.parked_at == 7
    assert mid.to_canonical()["parked"] is True
    log.settle("i1", outcome="escalated")
    end = fold(log.events)
    assert end.parked is False and end.parked_at is None
    assert end.last_settle == {"seq": 9, "input_id": "i1", "outcome": "escalated"}


def test_memory_session_scope_only():
    log = Log()
    log.start()
    log.input("i1", "task")
    log.turn("t1")
    log.response("t1", calls=[("c1", "bash", {})])
    log.call("t1", "c1", "bash", "guest")
    log.add("memory", {"scope": "session", "op": "write", "slug": "notes",
                       "sha256": "a" * 64, "derived_from": "c1"})
    log.add("memory", {"scope": "user", "op": "write", "slug": "prefs",
                       "sha256": "b" * 64, "derived_from": "c1"})
    assert fold(log.events).session_memory == {"notes": "a" * 64}
    assert fold(log.events).counters["user_memory"] == 1
    log.add("memory", {"scope": "session", "op": "remove", "slug": "notes",
                       "sha256": "a" * 64, "derived_from": "c1"})
    assert fold(log.events).session_memory == {}
    assert validate_log(log.events) == []


def test_usage_and_limits_accumulate():
    state = fold(golden("device_tool_late_duplicate"))
    assert state.usage == {"prompt_tokens": 350, "completion_tokens": 45}
    assert state.limits_consumed == {"turns": 2, "ops": 1}
    state = fold(golden("text_answer"))
    assert state.usage == {"prompt_tokens": 120, "completion_tokens": 9}
    assert state.limits_consumed == {"turns": 1, "ops": 0}


def test_op_state_and_tool_result_update_the_call():
    events = golden("mcp_tool_via_gateway")
    state = fold(events)
    call = state.open_calls["call_golden_mcp_01"]
    assert call["state"] == "done" and call["duration_ms"] == 4200
    assert call["result"] == 11 and call["spill_ref"] == "spill/call_golden_mcp_01.txt"
    assert state.counters["bulk"] == 1 and state.counters["bulk_bytes"] > 0
    assert state.tools == events[0]["body"]["tools"]
    running = fold(events, upto_seq=8).open_calls["call_golden_mcp_01"]
    assert running["state"] == "running" and "result" not in running


def test_rewrite_replaces_content_in_the_tool_message():
    log = Log()
    log.start()
    log.input("i1", "task")
    log.turn("t1")
    log.response("t1", calls=[("c1", "bash", {})])
    log.call("t1", "c1", "bash", "guest")
    log.result("c1", "raw secret", rewrite="[sanitised]")
    log.turn("t2")
    tool = [m for m in fold(log.events).committed_prefix if m["role"] == "tool"]
    assert tool == [{"role": "tool", "tool_call_id": "c1", "content": "[sanitised]"}]


def test_clock_error_and_bulk_are_recorded():
    log = Log()
    log.start()
    log.clock(5, kind="host_hello")
    log.add("error", {"class": "transient", "message": "vllm 503", "retry": True})
    log.add("bulk", {"kind": "workspace", "path": "ws.tar", "sha256": "d" * 64, "bytes": 7})
    log.clock(9, kind="grace_timer")
    log.add("wake", {"reason": "grace", "result_ids": [], "input_ids": []})
    state = fold(log.events)
    assert state.clock_at == 9
    assert state.counters["errors"] == 1 and state.counters["bulk_bytes"] == 7
    assert state.counters["wakes"] == 1


def test_turn_inputs_is_the_prefix_as_of_includes_through_seq():
    events = golden("device_tool_late_duplicate")
    turns = list(fold(events).turns)
    first = turn_inputs(events, turns[0])
    second = turn_inputs(events, turns[1])
    assert [m["role"] for m in first] == ["user"]
    assert [m["role"] for m in second] == ["user", "assistant", "tool"]
    assert canonical_json(first) == canonical_json(second[:1])      # the prefix only grows
    assert fold(events).committed_prefix == second
    with pytest.raises(ValueError):
        turn_inputs(events, "turn_missing")


def test_turn_start_commits_only_what_it_includes():
    log = Log()
    log.start()
    log.input("i1", "one")                                      # 2
    log.input("i2", "two", kind="follow_up")                    # 3
    log.turn("t1", includes=2)                                  # 4: built before seq 3 arrived
    state = fold(log.events)
    assert [m["content"] for m in state.committed_prefix] == ["one"]
    assert [it["message"]["content"] for it in state.staged_suffix] == ["two"]
    assert state.queues["follow_up"] == [3]
    assert [it["seq"] for it in state.committed] == [2]


def test_preamble_marker_on_first_user_message_of_each_turn():
    log = Log()
    log.start()
    log.input("i1", "one")
    log.input("i2", "two", kind="follow_up")
    log.turn("t1", preamble="pre-A")
    log.response("t1", "a")
    log.input("i3", "three", kind="follow_up")
    log.turn("t2", preamble="pre-B")
    users = [m for m in fold(log.events).committed_prefix if m["role"] == "user"]
    assert [m["preamble"] for m in users] == ["pre-A", None, "pre-B"]


def test_state_canonical_is_hashable_and_stable():
    state = fold(golden("mcp_tool_via_gateway"))
    canon = state.to_canonical()
    assert isinstance(canon["seen_input_ids"], list)
    assert isinstance(canon["placeholders"], list)
    assert isinstance(canon["turns"], list) and canon["turns"][0]["turn_id"] in state.turns
    assert canon["committed_prefix"] == state.committed_prefix
    assert canon["prefix_origins"] == state.prefix_origins
    assert canon["parked"] is False
    assert "committed" not in canon and not any(k.startswith("_") for k in canon)
    assert sha256_hex(canonical_json(canon)) == state_hash(state)
    assert state_hash(fold(golden("mcp_tool_via_gateway"))) == state_hash(state)
    assert state_hash(fold(golden("text_answer"))) != state_hash(state)


def test_state_views_and_last_turn():
    events = golden("device_tool_late_duplicate")
    state = fold(events)
    assert state.last_turn()["turn_id"] == list(state.turns)[-1]
    assert State().last_turn() is None
    assert len(state.committed) == len(state.committed_prefix) == len(state.prefix_origins)
    assert all(set(it) == {"seq", "origin", "message"} for it in state.committed)
