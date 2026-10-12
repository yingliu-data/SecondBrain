"""guest.taskview — one reducer for live handling and restore.

The autouse `verify_task_views` (conftest) compares the live TaskView with the
restored one for every Scenario whose loop returned. These tests do it in the
middle of a task too (parked, a side call or a question open, async calls
running, an mcp op suspended on an input request), plus reducer units.
"""
from __future__ import annotations

import time

from guest import hooks as hooks_mod
from guest.taskview import DOOM_LOOP_REPEATS, TaskView
from protocol.fold import State, apply
from tests.fakes.coordinator import (
    DEVICE, GRACE_S, QUESTION_S, Scenario, make_cfg, restored_view, text, tool_calls, view_problems,
    view_snapshot,
)
from tests.logbuilder import Log

PIN = "ab" * 32
MCP = [{"name": "wcc-events__draft", "side": "mcp", "pin": PIN}]


def quiet_problems(scn) -> list[str]:
    """view_problems once the loop is idle (blocked on an empty inbound queue)."""
    scn.wait_for(scn.inbound.empty)
    time.sleep(0.05)
    scn.wait_for(scn.inbound.empty)
    return view_problems(scn)


# ---------------------------------------------------------------- mid-task: live == restored
def test_parked_with_a_side_call_open():
    scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c_dev", "calendar", {})]), text("done")])
    scn.task("x")
    scn.start()
    scn.wait_for(lambda: any(e["body"].get("side_ref") for e in scn.of("op_state")))
    scn.control("park", {"reason": "idle"})
    scn.wait_event("parked")
    assert quiet_problems(scn) == []
    view = scn.coord.view
    assert view.parked and view.in_stretch and list(view.side_calls) == ["c_dev"]
    assert view.turn_pending == {"c_dev"} and view.last_response_had_calls
    scn.inbound.put(("link", "up"))
    scn.control("side_result", {"call_id": "c_dev", "side": "device", "content": "09:30", "ok": True})
    assert scn.run()["outcome"] == "done"
    assert not scn.coord.view.parked and scn.coord.view.task_input_id is None


def test_question_park_marks_the_question_expired_live_and_restored():
    cfg = make_cfg(answer_wait_s=3600)
    scn = Scenario(cfg=cfg, script=[tool_calls([("q1", "question", {"text": "a or b?"})]), text("ok")])
    scn.task("ask")
    scn.start()
    q = scn.wait_event("question")["body"]
    scn.wait_for(lambda: scn.clock.pending(QUESTION_S))
    scn.clock.fire(QUESTION_S)
    scn.wait_event("parked")
    assert quiet_problems(scn) == []
    view = scn.coord.view
    assert view.questions[q["question_id"]]["expired"] and view.questions[q["question_id"]]["kind"] == "question"
    assert view.park_question == q["question_id"] and view.question_left_ms == {q["question_id"]: 0}
    scn.inbound.put(("link", "up"))
    scn.control("ui_response", {"input_id": "ui-1", "question_id": q["question_id"], "answer": "a"})
    assert scn.run()["outcome"] == "done"
    assert scn.coord.view.questions == {}


def test_async_calls_running_and_results_unseen():
    cfg = make_cfg(async_tools=True)
    scn = Scenario(cfg=cfg, ops_manual=True, script=[
        tool_calls([("c1", "bash", {"command": "a"}), ("c2", "bash", {"command": "b"})]),
        text("waiting"), text("all done")])
    scn.task("x")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    scn.ops.complete("c1", "one")
    scn.wait_event("tool_result")
    assert quiet_problems(scn) == []
    view = scn.coord.view
    assert view.turn_pending == {"c2"} and view.unseen == ["c1"] and view.turn_calls == ["c1", "c2"]
    scn.clock.fire(GRACE_S)                           # grace: c1 wakes the model
    scn.wait_event("turn_start", 2)
    scn.wait_event("llm_response", 2)
    assert quiet_problems(scn) == []
    scn.ops.complete("c2", "two")
    assert scn.run()["outcome"] == "done"


def test_mcp_op_suspended_on_an_input_request():
    scn = Scenario(cfg=make_cfg(MCP), ops_manual=True, script=[
        tool_calls([("m1", "wcc-events__draft", {"title": "x"})]), text("drafted")])
    scn.task("draft it")
    scn.start()
    scn.wait_for(lambda: scn.ops.mcp_submitted)
    schema = {"type": "object", "properties": {"cal": {"type": "string", "enum": ["work", "home"]}}}
    scn.inbound.put(("op_input", "m1", {"request_state": "rs-1", "schema": schema,
                                        "message": "Which calendar?"}))
    q = scn.wait_event("question")["body"]
    assert q["options"] == ["work", "home"]
    assert quiet_problems(scn) == []
    assert scn.coord.view.questions[q["question_id"]]["kind"] == "mcp_input"
    scn.control("ui_response", {"input_id": "ui-1", "question_id": q["question_id"], "answer": "work"})
    scn.wait_for(lambda: len(scn.ops.mcp_submitted) == 2)
    assert scn.ops.mcp_submitted[1][3:5] == (1, {"request_state": "rs-1", "action": "accept",
                                                  "content": {"cal": "work"}})
    assert quiet_problems(scn) == []
    scn.ops.complete("m1", "Drafted x in work")
    assert scn.run()["outcome"] == "done"


def test_guard_ask_on_the_question_tool_is_an_ask_then_the_question():
    """The kind comes from the log: a hook's "ask" before the call runs, then
    (continue) the `question` built-in itself — restored the same way."""
    hooks = hooks_mod.Hooks()
    hooks.register_before(lambda state, call: ("ask", "confirm") if call["name"] == "question"
                          else ("allow", "ok"))
    scn = Scenario(hooks=hooks, script=[tool_calls([("q1", "question", {"text": "a or b?"})]), text("ok")])
    scn.task("x")
    scn.start()
    ask = scn.wait_event("question")["body"]
    assert quiet_problems(scn) == []
    assert scn.coord.view.questions[ask["question_id"]]["kind"] == "ask"
    scn.control("ui_response", {"input_id": "ui-1", "question_id": ask["question_id"], "answer": "continue"})
    real = scn.wait_event("question", 2)["body"]
    assert quiet_problems(scn) == []
    assert scn.coord.view.questions == {real["question_id"]: {
        "call_id": "q1", "kind": "question", "name": "question", "args": {"text": "a or b?"},
        "side": "guest", "expired": False}}
    scn.control("ui_response", {"input_id": "ui-2", "question_id": real["question_id"], "answer": "b"})
    assert scn.run()["outcome"] == "done"


# ---------------------------------------------------------------- reducer units
def fold_view(events, cfg=None) -> TaskView:
    cfg = cfg or make_cfg(DEVICE, session_id="sess-log")
    state, view = State(), TaskView(cfg, lambda: 0)
    for ev in events:
        apply(state, ev)
        view.apply(state, ev)
    return view


def test_doom_loop_counts_only_calls_after_a_hooks_decision():
    log = Log("sess-log")
    log.start()
    log.input("in-1", "go")
    log.turn("t1")
    calls = [(f"c{i}", "calendar", {"d": 1}) for i in range(DOOM_LOOP_REPEATS)]
    log.response("t1", calls=calls)
    log.add("guard_decision", {"ref": "c0", "decision": "deny", "reason": "limit", "policy_id": "limits"})
    log.call("t1", "c0", "calendar", "device", {"d": 1})
    assert fold_view(log.events).call_counts == {}     # not a call the doom-loop check saw
    for cid, _, args in calls[1:]:
        log.add("guard_decision", {"ref": cid, "decision": "allow", "reason": "ok", "policy_id": "hooks"})
        log.call("t1", cid, "calendar", "device", args)
    view = fold_view(log.events)
    assert list(view.call_counts.values()) == [DOOM_LOOP_REPEATS - 1]
    assert view.doom_loop("calendar", {"d": 1}) and not view.doom_loop("calendar", {"d": 2})
    assert view.turn_calls == [c[0] for c in calls] and view.last_response_had_calls


def test_settle_closes_the_task_and_keeps_the_manifest():
    log = Log("sess-log")
    log.start()
    log.input("in-1", "go")
    log.turn("t1")
    log.response("t1", content="hi")
    log.settle("in-1", answer="hi",
               manifest={"files": [{"path": "a.txt", "bytes": 1, "sha256": "f" * 64, "mode": 420}]})
    view = fold_view(log.events)
    assert view.task_input_id is None and view.turns == 1 and view.open_turn is None
    assert view.pending_inputs == [] and view.turn_pending == set()
    assert view.settled_files == {"a.txt": "f" * 64}


def test_restored_view_equals_a_reducer_fold():
    """restore() is the reducer over the log (nothing on the side but the preamble registry)."""
    scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c_dev", "calendar", {})]), text("done")])
    scn.answer_side_calls_with("09:30")
    scn.task("x")
    scn.run()
    assert view_snapshot(restored_view(scn.cfg, scn.events)) == view_snapshot(fold_view(scn.events, scn.cfg))
