"""protocol.fold's host index fields (call_args, question_text,
settles_by_input, open_input) and protocol.replay's SessionIndex: derived
from the events, never part of the state hash."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from protocol.fold import State, apply, apply_valid, fold, state_hash, verify_checkpoints
from protocol.replay import SessionIndex, session_index

from .logbuilder import Log

GOLDEN = Path(__file__).resolve().parent / "golden"
NAMES = ("text_answer", "device_tool_late_duplicate", "mcp_tool_via_gateway")


def golden(name: str) -> list[dict]:
    with open(GOLDEN / f"{name}.jsonl", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _question_log() -> Log:
    log = Log()
    log.start()
    log.input("in_1", "ask me")
    log.turn("t1")
    log.response("t1", calls=[("c_q", "question", {"text": "Which?"}),
                              ("c_d", "calendar", {"day": "mon"})])
    log.call("t1", "c_q", "question", "guest", args={"text": "Which?"})
    log.call("t1", "c_d", "calendar", "device", args={"day": "mon"})
    log.add("question", {"question_id": "q1", "call_id": "c_q", "text": "Which?",
                         "options": ["a", "b"], "deadline": 5})
    return log


@pytest.mark.parametrize("name", NAMES)
def test_goldens_checkpoints_still_verify(name):
    """The index fields are not hashed: every checkpoint the goldens carry
    (hashed before the fields existed) still matches."""
    assert verify_checkpoints(golden(name)) == []


def test_index_fields_follow_the_events():
    log = _question_log()
    state = fold(log.events)
    assert state.call_args == {"c_q": {"text": "Which?"}, "c_d": {"day": "mon"}}
    assert state.question_text == {"q1": {"text": "Which?", "options": ["a", "b"]}}
    assert state.open_input == "in_1" and state.settles_by_input == {}
    log.input("in_2", "more", kind="follow_up")          # staged into the running task
    assert fold(log.events).open_input == "in_1"
    log.result("c_q", "a")
    log.result("c_d", "ok")
    settle = log.settle("in_1", answer="done it")
    state = fold(log.events)
    assert state.open_input is None
    assert state.settles_by_input == {"in_1": settle["body"]}
    log.input("in_3", "next")
    assert fold(log.events).open_input == "in_3"


def test_duplicate_and_ui_response_inputs_do_not_open_a_task():
    log = Log()
    log.start()
    log.input("in_1", "x")
    log.settle("in_1")
    log.input("in_1", "x", dedupe="duplicate")
    assert fold(log.events).open_input is None


def test_index_fields_never_change_the_hash():
    log = _question_log()
    state = fold(log.events)
    before = state_hash(state)
    canon = state.to_canonical()
    for name in ("call_args", "question_text", "settles_by_input", "open_input",
                 "_unresolved", "_checkpoint_seqs"):
        assert name not in canon
    state.call_args["x"] = {"y": 1}
    state.question_text.clear()
    state.settles_by_input["z"] = {}
    state.open_input = "other"
    assert state_hash(state) == before
    assert state == fold(log.events)          # nor the comparison


def test_to_canonical_is_a_copy():
    state = fold(_question_log().events)
    canon = state.to_canonical()
    canon["open_calls"]["c_q"]["state"] = "mutated"
    canon["seen_input_ids"].append("zz")
    assert state.open_calls["c_q"]["state"] == "RUNNING"
    assert "zz" not in state.seen_input_ids


def test_apply_valid_is_apply_without_the_schema_check():
    events = golden("device_tool_late_duplicate")
    a, b = State(), State()
    for ev in events:
        apply(a, ev)
        apply_valid(b, ev)
    assert state_hash(a) == state_hash(b)


def _write_run(session: Path, run: int, events: list[dict]) -> None:
    d = session / "events" / f"r{run:04d}"
    d.mkdir(parents=True)
    (d / "000001.jsonl").write_text("".join(json.dumps(e, separators=(",", ":")) + "\n"
                                            for e in events))


def test_session_index_scans_runs_cheaply_and_matches_the_fold(tmp_path):
    first = Log()
    first.start()
    first.input("in_1", "hi")
    first.turn("t1")
    first.response("t1", content="hello")
    s1 = first.settle("in_1", answer="hello")
    second = Log()
    second.start()
    second.input("in_1", "hi", dedupe="duplicate")
    second.input("in_2", 'a "settle" in the text')
    second.clock(3)
    _write_run(tmp_path, 1, first.events)
    _write_run(tmp_path, 2, second.events)
    idx = session_index(tmp_path)
    assert idx.seen_input_ids == {"in_1", "in_2"}
    assert idx.settles == {"in_1": s1["body"]} and idx.settled("in_1") == s1["body"]
    assert session_index(tmp_path, runs=[2]).settles == {}
    # the same as folding each run and adding the states
    folded = SessionIndex().add_state(fold(first.events)).add_state(fold(second.events))
    assert folded.seen_input_ids == idx.seen_input_ids and folded.settles == idx.settles
