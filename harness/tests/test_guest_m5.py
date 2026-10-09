"""M5 guest: questions park the task, escalation via abort (docs/M5-CONTRACTS.md
section 4, PLAN.md D12).

Coordinator scenarios with the fakes of test_guest_coordinator (manual clock),
then guest.main as a subprocess against FakeHost / FakeLLM / FakeGateway.
Every resulting log validates, replays and its checkpoints verify.
"""
from __future__ import annotations

import time

from guest.config import from_hello
from protocol.events import UNANSWERED, validate_event
from protocol.fold import state_hash, verify_checkpoints
from tests.test_guest_coordinator import (
    DEVICE, QUESTION_S, WALL_S, Scenario, make_cfg, text, tool_calls,
)
from tests.test_guest_e2e import check_replay
from tests.test_guest_m3 import check_parked_log, m3_hello, rig  # noqa: F401 (fixture)
from tests.test_guest_m3_units import full_check
from tests.test_guest_m4 import m4_hello
from tests.test_guest_m4 import run as run_m4
from tests.test_guest_m4 import settle_of

WAIT_S = 86_400


def q_call(cid="q1", text_="Which one?"):
    return tool_calls([(cid, "question", {"text": text_, "options": ["a", "b"]})])


def m5_cfg(*extra, answer_wait_s=WAIT_S, **over):
    cfg = make_cfg(extra, **over)
    cfg.limits.answer_wait_s = answer_wait_s
    return cfg


def answer(scn, qid, text_="a", input_id=None):
    scn.control("ui_response", {"input_id": input_id or f"ui-{qid}", "question_id": qid, "answer": text_})


def park_on_question(scn):
    """Run until the question, fire its deadline, wait for the park."""
    scn.start()
    q = scn.wait_event("question")["body"]
    assert scn.clock.fire(QUESTION_S) == 1
    parked = scn.wait_event("parked")
    return q, parked


def check(events):
    state = full_check(events)
    assert verify_checkpoints(events) == []
    return state


# ---------------------------------------------------------------- config
def test_answer_wait_s_is_a_hello_limit_in_the_config_hash():
    body = m3_hello()
    base = from_hello(body)
    assert base.limits.answer_wait_s == 0
    body["config"]["limits"]["answer_wait_s"] = 3600
    cfg = from_hello(body)
    assert cfg.limits.answer_wait_s == 3600
    assert cfg.config_hash() != base.config_hash()
    body["config"]["limits"]["answer_wait_s"] = -1
    try:
        from_hello(body)
    except ValueError as exc:
        assert "answer_wait_s" in str(exc)
    else:
        raise AssertionError("a negative answer_wait_s must be rejected")


def test_parked_question_id_and_wall_used_ms_are_optional_body_keys():
    ok = {"session_id": "s", "seq": 3, "ts": "t", "schema": 1, "type": "parked",
          "body": {"checkpoint_seq": 2, "reason": "question", "question_id": "q-x"}}
    assert validate_event(ok) == []
    bad = dict(ok, body={"checkpoint_seq": 2, "reason": "question", "question_id": 7})
    assert validate_event(bad)
    assert validate_event(dict(ok, body={**ok["body"], "wall_used_ms": 0})) == []
    assert validate_event(dict(ok, body={**ok["body"], "wall_used_ms": -1}))
    assert validate_event(dict(ok, body={**ok["body"], "wall_used_ms": "1"}))


# ---------------------------------------------------------------- coordinator
def test_deadline_parks_with_question_id_then_answer_after_resume_completes():
    scn = Scenario(cfg=m5_cfg(), script=[q_call(), text("You chose a.")])
    scn.task("ask me")
    q, parked = park_on_question(scn)
    qid = q["question_id"]
    types = scn.types()
    i = types.index("question")
    assert types[i:] == ["question", "clock", "checkpoint", "parked"]
    assert scn.bodies("clock")[-1]["kind"] == "question_deadline"
    assert parked["body"] == {"checkpoint_seq": parked["seq"] - 1, "reason": "question", "question_id": qid,
                              "wall_used_ms": parked["body"]["wall_used_ms"]}
    assert "guard_decision" not in types[i:] and scn.state.denied_questions == 0
    assert scn.state.open_questions[qid]["answered"] is False      # the question stays open
    assert scn.transport.parks == 1 and scn.coord.parked
    assert scn.clock.pending() == []                               # every timer paused / gone
    check(scn.events)
    # resume (same process: the snapshot) and answer
    scn.inbound.put(("link", "up"))
    assert scn.clock.pending(QUESTION_S) == []                     # the expired deadline is not re-armed
    answer(scn, qid, "a")
    result = scn.run()
    assert result["outcome"] == "done" and result["answer"] == "You chose a."
    assert scn.bodies("tool_result")[0] == {"call_id": "q1", "content": "a"}
    assert scn.types()[types.index("parked") + 1:] == [
        "input", "op_state", "tool_result", "wake", "clock", "turn_start", "llm_response", "settle"]
    assert len(scn.provider.requests) == 2                         # the model was woken
    state = check(scn.events)
    assert state_hash(state) == state_hash(scn.state)
    assert state.open_questions[qid]["answered"] is True


def test_answer_wait_zero_keeps_the_deny():
    scn = Scenario(cfg=m5_cfg(answer_wait_s=0), script=[q_call(), text("I will guess.")])
    scn.task("ask")
    scn.start()
    scn.wait_event("question")
    assert scn.clock.fire(QUESTION_S) == 1
    assert scn.run()["outcome"] == "done"
    assert "parked" not in scn.types()
    assert scn.bodies("guard_decision")[-1]["reason"] == UNANSWERED
    check(scn.events)


def test_zero_timeout_without_ui_parks_at_once():
    scn = Scenario(cfg=m5_cfg(question_timeout_s=0, ui_attached=False), script=[q_call(), text("b then")])
    scn.task("ask")
    scn.start()
    parked = scn.wait_event("parked")
    qid = scn.bodies("question")[0]["question_id"]
    assert parked["body"]["question_id"] == qid
    assert scn.types()[-3:] == ["question", "checkpoint", "parked"]
    scn.inbound.put(("link", "up"))
    answer(scn, qid, "b")
    assert scn.run()["answer"] == "b then"
    check(scn.events)


def test_fresh_process_resume_then_answer_completes_and_wall_clock_skips_the_park():
    a = Scenario(cfg=m5_cfg(), script=[q_call()])
    a.task("ask")
    q, _ = park_on_question(a)
    log = list(a.events)
    b = Scenario(cfg=m5_cfg(resume=True), script=[text("Resumed with a.")])
    b.clock.ms = a.clock.ms + 3_600_000 * 20                   # parked for 20 hours
    b.coord.restore(log)
    assert b.coord._waiting == {q["question_id"]}
    b.start()
    b.wait_for(lambda: b.events)
    assert [e["type"] for e in b.events] == ["clock"]          # host_hello only
    assert b.clock.pending(QUESTION_S) == []                   # parked on it: no deadline timer
    wall = [h.delay_s for h in b.clock.pending()]
    assert len(wall) == 1 and WALL_S - 5 < wall[0] <= WALL_S   # the park did not use the wall clock
    answer(b, q["question_id"], "a")
    result = b.run()
    assert result["outcome"] == "done" and result["answer"] == "Resumed with a."
    assert b.bodies("tool_result")[0] == {"call_id": "q1", "content": "a"}
    state = check(log + b.events)
    assert state_hash(state) == state_hash(b.state)


def test_parked_records_the_wall_clock_used_and_restore_reads_it():
    """parked.wall_used_ms is the task's wall clock up to the park; a fresh
    process restoring after a resume adds only the guest time since the first
    guest clock event after that park."""
    a = Scenario(cfg=m5_cfg(*DEVICE), script=[q_call(), tool_calls([("c_dev", "calendar", {})])])
    a.task("ask")
    a.start()
    a.wait_event("question")
    a.clock.advance(30_000)
    assert a.clock.fire(QUESTION_S) == 1
    used = a.wait_event("parked")["body"]["wall_used_ms"]
    assert 30_000 <= used < 31_000
    a.clock.advance(3_600_000)                                 # parked for an hour
    a.inbound.put(("link", "up"))
    answer(a, a.bodies("question")[0]["question_id"], "a")
    a.wait_for(lambda: any(e["body"].get("side_ref") for e in a.of("op_state")))
    a.clock.advance(10_000)                                    # live after the resume, then death
    log = list(a.events)
    b = Scenario(cfg=m5_cfg(*DEVICE, resume=True), script=[text("done")])
    b.clock.ms = a.clock.ms
    b.coord.restore(log)
    b.start()
    b.wait_event("settle")
    delays = [h.delay_s for h in b.clock.handles if h.delay_s > QUESTION_S]
    assert WALL_S - 41 < delays[0] <= WALL_S - 40             # 30 s before the park + 10 s after


def test_two_questions_past_their_deadline_park_one_after_the_other():
    """A bash op of the same round holds the park, so both deadlines pass
    before it: the task parks on the first, and once that is answered, on
    the second."""
    scn = Scenario(cfg=m5_cfg(), ops_manual=True, script=[
        tool_calls([("c1", "bash", {"command": "sleep 1"}),
                    ("q1", "question", {"text": "one?"}), ("q2", "question", {"text": "two?"})]),
        text("Both answered.")])
    scn.task("ask twice")
    scn.start()
    scn.wait_event("question", 2)
    q1, q2 = (b["question_id"] for b in scn.bodies("question"))
    assert scn.clock.fire(QUESTION_S) == 2
    scn.wait_for(lambda: len([c for c in scn.bodies("clock") if c["kind"] == "question_deadline"]) == 2)
    assert "parked" not in scn.types()                          # the bash op holds the park
    scn.ops.complete("c1", "slept")
    scn.wait_event("parked")
    assert scn.bodies("parked")[0]["question_id"] == q1
    scn.inbound.put(("link", "up"))
    answer(scn, q1, "first")
    scn.wait_event("parked", 2)                                 # still waiting on q2: park again
    assert scn.bodies("parked")[1]["question_id"] == q2
    scn.inbound.put(("link", "up"))
    answer(scn, q2, "second")
    assert scn.run()["answer"] == "Both answered."
    assert [b["content"] for b in scn.bodies("tool_result")] == ["slept", "first", "second"]
    check(scn.events)


def test_abort_escalated_after_resume_settles_escalated():
    scn = Scenario(cfg=m5_cfg(), script=[q_call()])
    scn.task("ask")
    park_on_question(scn)
    scn.inbound.put(("link", "up"))                             # the host wakes it to deliver
    scn.control("abort", {"reason": "escalated"})
    result = scn.run()
    assert result["outcome"] == "escalated" and result["answer"] == ""
    assert result["reason"] == "abort" and result["detail"] == "escalated"
    assert scn.bodies("op_state")[-1] == {"call_id": "q1", "state": "killed"}
    assert scn.types()[-1] == "settle"
    check(scn.events)


def test_abort_aborted_after_fresh_process_resume_settles_aborted():
    a = Scenario(cfg=m5_cfg(), script=[q_call()])
    a.task("ask")
    park_on_question(a)
    log = list(a.events)
    b = Scenario(cfg=m5_cfg(resume=True))
    b.coord.restore(log)
    b.start()
    b.wait_for(lambda: b.events)
    b.control("abort", {"reason": "aborted"})
    result = b.run()
    assert result["outcome"] == "aborted" and result["answer"] == ""
    assert result["reason"] == "abort" and result["detail"] == "aborted"
    assert b.events[-1]["body"]["outcome"] == "aborted"
    check(log + b.events)


def test_abort_while_running_maps_the_reason():
    for reason, outcome in (("escalated", "escalated"), ("user cancelled", "aborted")):
        scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c_dev", "calendar", {})])])
        scn.task("x")
        scn.start()
        scn.wait_for(lambda: any(e["body"].get("side_ref") for e in scn.of("op_state")))
        scn.control("abort", {"reason": reason})
        result = scn.run()
        assert (result["outcome"], result["answer"], result["detail"]) == (outcome, "", reason)
        check(scn.events)


# ---------------------------------------------------------------- end to end
def test_e2e_question_deadline_parks_then_answer_after_reconnect(rig):
    hello = m3_hello(answer_wait_s=WAIT_S)
    hello["config"]["question_timeout_s"] = 1
    r = rig(hello, script=[q_call(), text("Got it.")])
    proc = r.spawn()
    r.task()
    q = r.host.wait_for("question", timeout=10)["body"]
    parked = r.host.wait_for("parked", timeout=10)["body"]
    assert parked["reason"] == "question" and parked["question_id"] == q["question_id"]
    r.host.wait_disconnected(timeout=5)
    r.host.listen()                                             # the restore
    r.host.wait_connected(timeout=10, count=2)
    r.host.send("ui_response", {"input_id": "ui-1", "question_id": q["question_id"], "answer": "a"})
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc) == 0, r.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "Got it."
    assert r.of("tool_result")[0]["content"] == "a"
    assert not [g for g in r.of("guard_decision") if g["reason"] == UNANSWERED]
    check_parked_log(r.host.events)


def test_e2e_fresh_process_resume_then_abort_escalated(rig):
    hello = m3_hello(answer_wait_s=WAIT_S)
    hello["config"]["question_timeout_s"] = 1
    r = rig(hello, script=[q_call()])
    proc = r.spawn()
    r.task()
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)
    r.kill(proc)                                                # the host stopped the parked VM
    r.host.listen()
    proc2 = r.spawn(wait_hello=False)
    r.host.wait_connected(timeout=10, count=r.host.connections + 1)
    hello_msg = r.host.send("hello", dict(r.hello, resume=True))
    r.host.wait_acked_cseq(hello_msg["cseq"], timeout=10)
    r.host.send("abort", {"reason": "escalated"})
    settle = r.host.wait_for("settle", timeout=15)["body"]
    _, err = proc2.communicate(timeout=20)
    assert proc2.returncode == 0, err
    assert settle["outcome"] == "escalated" and settle["reason"] == "abort"
    assert len(r.of("session_start")) == 1
    check_parked_log(r.host.events)


def test_e2e_input_required_deadline_parks_without_cancel(rig):
    r, proc = run_m4(rig, [tool_calls([("call_p", "wcc-events__approve", {})]), text("Done.")],
                     m4_hello(question_timeout_s=1, answer_wait_s=WAIT_S))
    q = r.host.wait_for("question", timeout=10)["body"]
    parked = r.host.wait_for("parked", timeout=10)["body"]
    assert parked == {**parked, "reason": "question", "question_id": q["question_id"]}
    r.host.wait_disconnected(timeout=5)
    time.sleep(0.2)
    assert len(r.host.gateway.requests) == 1                    # no cancel re-send: still suspended
    r.host.listen()
    r.host.wait_connected(timeout=10, count=2)
    r.host.send("ui_response", {"input_id": "ui-p", "question_id": q["question_id"], "answer": "approve"})
    settle = settle_of(r, proc)
    assert settle["answer"] == "Done."
    assert r.of("tool_result")[0]["content"] == "approved: approve"
    assert [(b["state"], b.get("attempt")) for b in r.of("op_state")] == [
        ("running", None), ("input_required", 0), ("running", 1), ("done", None)]
    assert r.host.gateway.requests[-1]["body"]["params"]["_meta"]["sb/input_response"]["action"] == "accept"
    assert not r.of("guard_decision") or all(g["reason"] != UNANSWERED for g in r.of("guard_decision"))
    check_parked_log(r.host.events)


def test_e2e_input_required_answer_wait_zero_still_cancels(rig):
    r, proc = run_m4(rig, [tool_calls([("call_c", "wcc-events__approve", {})]), text("Cancelled.")],
                     m4_hello(question_timeout_s=1))
    settle = settle_of(r, proc)
    assert settle["answer"] == "Cancelled." and "parked" not in r.types()
    assert r.host.gateway.requests[-1]["body"]["params"]["_meta"]["sb/input_response"]["action"] == "cancel"
    check_replay(r.host.events)
