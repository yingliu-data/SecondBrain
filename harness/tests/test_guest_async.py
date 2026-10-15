"""Async tool calls (hello.config.async_tools = true; docs/GUEST.md "Async
mode", docs/ASYNC-TOOLS.md).

Units drive the coordinator with the fakes of tests.fakes.coordinator (manual
ops, a fake clock whose grace timer the test fires); the e2e runs the guest
subprocess against FakeHost and a FakeLLM scripted for async. Every log must
validate, replay (its state equal to the coordinator's), verify its
checkpoints, and reproduce each turn's request_hash from the log alone
(`rebuilt_request_hashes`).
"""
from __future__ import annotations

import time

from guest.context import (
    ASYNC_TOOLS_NOTE, LATE_RESULT_HEADER, build_compaction_request, build_request, system_block,
    system_hash,
)
from guest.coordinator import LOST_CONTENT
from protocol.fold import PLACEHOLDER, fold, verify_checkpoints
from tests.fakes.coordinator import (
    BIG, DEVICE, GRACE_S, LONG_TASK, Scenario, T0, check_log, make_cfg, sized, text, tool_calls,
)
from tests.test_guest_e2e import guest_factory  # noqa: F401  (fixture)
from tests.test_guest_m3_units import full_check, park_scenario


def acfg(extra_tools=(), grace=None, **over):
    """An async-mode config (grace = limits.grace_s; GRACE_S by default)."""
    cfg = make_cfg(extra_tools, async_tools=True, **over)
    if grace is not None:
        cfg.limits.grace_s = grace
    return cfg


def rebuilt_request_hashes(events: list, cfg) -> list:
    """(logged, rebuilt) request_hash per turn: each request rebuilt from the
    fold of the log just before its turn_start, the clock the turn_clock
    recorded, the attachments of the inputs and the preamble registry built
    turn by turn: what a replay does."""
    preambles: dict = {}
    attachments: dict = {}
    out = []
    for e in events:
        body = e["body"]
        if e["type"] == "input" and isinstance(body.get("attachments"), list):
            attachments[body["input_id"]] = body["attachments"]
        if e["type"] != "turn_start":
            continue
        state = fold(events, upto_seq=e["seq"] - 1)
        if body["type"] == "compaction":
            req = build_compaction_request(state, cfg, preambles=preambles)
        else:
            max_tools = cfg.limits.max_tools
            with_tools = not (max_tools and state.limits_consumed["ops"] >= max_tools)   # one task
            req = build_request(state, cfg, state.clock_at, attachments=attachments,
                                preambles=preambles, with_tools=with_tools)
        out.append((body["request_hash"], req.request_hash))
    return out


def check_async_log(scn) -> object:
    state = check_log(scn)
    pairs = rebuilt_request_hashes(scn.events, scn.cfg)
    assert pairs and all(a == b for a, b in pairs), pairs
    return state


def late(call_id: str, content: str, name: str = "bash") -> dict:
    return {"role": "user",
            "content": LATE_RESULT_HEADER.format(call_id=call_id, name=name) + "\n" + content}


def quiet(scn, seconds: float = 0.1) -> list:
    """The event types after the coordinator had time to act (asserting
    something does NOT happen)."""
    time.sleep(seconds)
    return scn.types()


def request(scn, n: int):
    """The n-th (1-based) provider request, once the provider thread made it."""
    scn.wait_for(lambda: len(scn.provider.requests) >= n)
    return scn.provider.requests[n - 1]


TWO_CALLS = tool_calls([("c1", "bash", {"command": "fast"}), ("c2", "bash", {"command": "slow"})])


# ---------------------------------------------------------------- system block
def test_async_mode_adds_the_fixed_note_to_the_system_block():
    sync, asyn = make_cfg(), acfg()
    assert ASYNC_TOOLS_NOTE not in system_block(sync)
    assert ASYNC_TOOLS_NOTE in system_block(asyn) and PLACEHOLDER in ASYNC_TOOLS_NOTE
    assert system_hash(sync) != system_hash(asyn)
    assert sync.config_hash() != asyn.config_hash()
    assert system_block(asyn) == system_block(acfg())          # fixed: cache-safe


# ---------------------------------------------------------------- wake rules
def test_one_of_two_calls_done_wakes_with_its_result_and_a_placeholder():
    scn = Scenario(cfg=acfg(), ops_manual=True,
                   script=[TWO_CALLS, text("Waiting for the slow one."), text("fast + slow")])
    scn.task("run both")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    assert scn.clock.pending(GRACE_S)                           # the grace window is open
    scn.ops.complete("c1", "fast out")
    scn.wait_event("tool_result")
    assert "wake" not in quiet(scn)                             # batched by the grace window
    scn.clock.fire(GRACE_S)
    t2 = scn.wait_event("turn_start", 2)
    i = scn.types().index("wake")
    assert scn.events[i - 1]["body"]["kind"] == "grace_expiry"
    assert scn.events[i]["body"] == {"reason": "grace", "result_ids": ["c1"], "input_ids": []}
    req2 = request(scn, 2)
    assert req2.messages[-3]["tool_calls"][1]["id"] == "c2"
    assert req2.messages[-2:] == [
        {"role": "tool", "tool_call_id": "c1", "content": "fast out"},
        {"role": "tool", "tool_call_id": "c2", "content": PLACEHOLDER}]
    # a response without tool calls while c2 runs does not settle
    scn.wait_event("llm_response", 2)
    assert "settle" not in quiet(scn)
    assert scn.state.placeholders == {"c2"} and t2["body"]["includes_through_seq"] == t2["seq"] - 1
    # c2's result lands after its committed placeholder: late, a new item, next turn
    scn.ops.complete("c2", "slow out")
    result = scn.run()
    assert result["outcome"] == "done" and result["answer"] == "fast + slow"
    assert scn.bodies("tool_result")[1] == {"call_id": "c2", "content": "slow out", "late": True}
    assert scn.bodies("wake")[1] == {"reason": "tool_result", "result_ids": ["c2"], "input_ids": []}
    req3 = request(scn, 3)
    assert req3.messages[: len(req2.messages)] == req2.messages          # the prefix is untouched
    assert req3.messages[len(req2.messages):] == [
        {"role": "assistant", "content": "Waiting for the slow one."}, late("c2", "slow out")]
    state = check_async_log(scn)
    assert state.counters["placeholder_fills"] == 1 and state.counters["late_results"] == 0
    assert state.limits_consumed["turns"] == 3


def test_without_a_grace_window_each_result_wakes_at_once():
    scn = Scenario(cfg=acfg(grace=0), ops_manual=True,
                   script=[TWO_CALLS, text("waiting"), text("done")])
    scn.task("run both")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    assert scn.clock.pending(0.0) == []                        # no grace timer armed
    scn.ops.complete("c1", "one")
    scn.wait_event("turn_start", 2)
    assert scn.bodies("wake") == [{"reason": "tool_result", "result_ids": ["c1"], "input_ids": []}]
    assert "grace_expiry" not in [b["kind"] for b in scn.bodies("clock")]
    scn.wait_event("llm_response", 2)
    scn.ops.complete("c2", "two")
    assert scn.run()["answer"] == "done"
    check_async_log(scn)


WALL = 300.0


def test_coincident_results_share_one_turn_and_match_the_sync_shape():
    """Results already queued when the loop looks (the slurp) are one wake:
    with instant tools async mode logs what sync mode logs."""
    logs = {}
    for mode in (False, True):
        cfg = acfg() if mode else make_cfg()
        scn = Scenario(cfg=cfg, script=[TWO_CALLS, text("both")])
        scn.task("run both")
        assert scn.run()["answer"] == "both"
        assert scn.bodies("wake") == [{"reason": "tool_result", "result_ids": ["c1", "c2"], "input_ids": []}]
        assert scn.state.placeholders == set()
        check_async_log(scn)
        logs[mode] = scn.types()
    assert logs[False] == logs[True]


def test_turn_without_calls_waits_and_settles_after_the_last_result():
    scn = Scenario(cfg=acfg(grace=0), ops_manual=True,
                   script=[TWO_CALLS, text("wait 1"), text("wait 2"), text("all in")])
    scn.task("run both")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    scn.ops.complete("c2", "two")                 # the second call finishes first
    scn.wait_event("llm_response", 2)
    assert "settle" not in quiet(scn)
    req2 = request(scn, 2)
    # positions follow the calls, not the arrival order
    assert req2.messages[-2:] == [
        {"role": "tool", "tool_call_id": "c1", "content": PLACEHOLDER},
        {"role": "tool", "tool_call_id": "c2", "content": "two"}]
    scn.ops.complete("c1", "one")
    result = scn.run()
    # c1 was the last one running: its late result woke the model once more, and
    # that turn's text answer (nothing running, nothing unseen) settled
    assert len(scn.of("turn_start")) == 3 and result["answer"] == "wait 2"
    assert scn.bodies("tool_result")[1]["late"] is True
    check_async_log(scn)


def test_new_input_wakes_the_model_while_calls_run():
    scn = Scenario(cfg=acfg(), ops_manual=True,
                   script=[tool_calls([("c1", "bash", {"command": "slow"})]),
                           text("noted, still waiting"), text("finished")])
    scn.task("run it")
    scn.start()
    scn.wait_for(lambda: scn.ops.submitted)
    scn.task("also say hi", input_id="in-2", kind="follow_up")   # inside the grace window
    scn.wait_event("turn_start", 2)
    in2 = scn.of("input")[1]["seq"]
    assert scn.bodies("wake") == [{"reason": "input", "result_ids": [], "input_ids": ["in-2"]}]
    req2 = request(scn, 2)
    assert req2.messages[-2] == {"role": "tool", "tool_call_id": "c1", "content": PLACEHOLDER}
    assert req2.messages[-1]["role"] == "user" and req2.messages[-1]["content"].endswith("also say hi")
    assert req2.preamble_input_id == "in-2" and in2 > scn.of("tool_call")[0]["seq"]
    scn.wait_event("llm_response", 2)
    scn.ops.complete("c1", "slow done")
    assert scn.run()["answer"] == "finished"
    assert scn.bodies("tool_result")[0]["late"] is True
    check_async_log(scn)


def test_more_calls_while_earlier_ones_run_respect_max_tools():
    scn = Scenario(cfg=acfg(grace=0, max_tools=3), ops_manual=True, script=[
        TWO_CALLS,
        tool_calls([("c3", "bash", {"command": "third"}), ("c4", "bash", {"command": "fourth"})]),
        text("waiting"), text("end")])
    scn.task("go")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    scn.ops.complete("c1", "one")
    scn.wait_for(lambda: len(scn.ops.submitted) == 3)          # c3 runs beside c2
    assert [c for c, _, _ in scn.ops.submitted] == ["c1", "c2", "c3"]
    # c4 is over max_tools: denied with an error result straight away; that
    # result wakes the model again (c2 and c3 still run, as placeholders)
    scn.wait_event("turn_start", 3)
    c4 = [b for b in scn.bodies("tool_result") if b["call_id"] == "c4"][0]
    assert "max_tools" in c4["content"]
    req3 = request(scn, 3)
    assert req3.tools == []                                    # ops >= max_tools: the wrap-up turn
    shown = {m["tool_call_id"]: m["content"] for m in req3.messages if m["role"] == "tool"}
    assert shown["c2"] == PLACEHOLDER and shown["c3"] == PLACEHOLDER
    scn.wait_event("llm_response", 3)
    assert "settle" not in quiet(scn)
    scn.ops.complete("c2", "two")
    scn.wait_event("llm_response", 4)
    assert "settle" not in quiet(scn)                          # c3 still runs
    scn.provider.push(text("really the end"))
    scn.ops.complete("c3", "three")
    result = scn.run()
    assert result["outcome"] == "done" and result["reason"] == "max_tools"
    assert result["answer"] == "really the end"
    assert all(r.tools == [] for r in scn.provider.requests[2:])
    check_async_log(scn)


def test_max_turns_with_calls_running_kills_them_and_fails():
    scn = Scenario(cfg=acfg(grace=0, max_turns=2), ops_manual=True,
                   script=[TWO_CALLS, text("waiting")])
    scn.task("go")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    scn.ops.complete("c1", "one")
    scn.wait_event("llm_response", 2)
    scn.task("and now?", input_id="in-2", kind="follow_up")   # a third turn: over max_turns
    result = scn.run()
    assert result["outcome"] == "failed" and result["reason"] == "max_turns"
    assert scn.bodies("tool_result")[-1] == {"call_id": "c2", "content": "error: killed", "late": True}
    assert scn.ops.aborted == 1
    check_async_log(scn)


def test_wall_clock_with_calls_running_closes_them():
    scn = Scenario(cfg=acfg(grace=0), ops_manual=True, script=[TWO_CALLS, text("waiting")])
    scn.task("go")
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    scn.ops.complete("c1", "one")
    scn.wait_event("llm_response", 2)
    scn.clock.fire(WALL)
    result = scn.run()
    assert result["outcome"] == "timeout"
    c2 = [b for b in scn.bodies("tool_result") if b["call_id"] == "c2"][0]
    assert c2["late"] is True and c2["content"] == "error: timeout"
    check_async_log(scn)


# ---------------------------------------------------------------- restore, park
def _async_log_with_a_placeholder(cfg, extra_script=()):
    """A run stopped after turn 2 answered 'waiting' with c2 still open."""
    a = Scenario(cfg=cfg, ops_manual=True, script=[TWO_CALLS, text("waiting"), *extra_script])
    a.task("run both")
    a.start()
    a.wait_for(lambda: len(a.ops.submitted) == 2)
    a.ops.complete("c1", "one")
    a.wait_event("llm_response", 2)
    quiet(a, 0.05)
    return a, list(a.events)


def test_restore_keeps_placeholders_and_the_lost_result_is_late():
    a, log = _async_log_with_a_placeholder(acfg(grace=0))
    b = Scenario(cfg=acfg(grace=0, resume=True, at=T0 + 5_000), script=[text("recovered")])
    b.coord.restore(log)
    assert b.state.placeholders == {"c2"}
    result = b.run()
    assert result["answer"] == "recovered"
    assert [e["type"] for e in b.events] == ["clock", "op_state", "tool_result", "wake", "clock",
                                             "turn_start", "llm_response", "settle"]
    assert b.events[2]["body"] == {"call_id": "c2", "content": LOST_CONTENT, "late": True}
    assert b.events[3]["body"] == {"reason": "tool_result", "result_ids": ["c2"], "input_ids": []}
    assert request(b, 1).messages[-1] == late("c2", LOST_CONTENT)
    events = log + b.events
    full_check(events)
    pairs = rebuilt_request_hashes(events, b.cfg)
    assert all(x == y for x, y in pairs) and len(pairs) == 3


def test_restore_with_an_unseen_result_wakes_on_it():
    a = Scenario(cfg=acfg(), ops_manual=True, script=[TWO_CALLS])
    a.task("run both")
    a.start()
    a.wait_for(lambda: len(a.ops.submitted) == 2)
    a.ops.complete("c1", "one")                 # inside the grace window: unseen; the VM dies
    a.wait_event("tool_result")
    log = list(a.events)
    b = Scenario(cfg=acfg(resume=True), script=[text("both seen")])
    b.coord.restore(log)
    assert b.run()["answer"] == "both seen"
    # c2 lost (no placeholder was committed: not late), one wake for both
    assert b.bodies("tool_result") == [{"call_id": "c2", "content": LOST_CONTENT}]
    assert b.bodies("wake") == [{"reason": "tool_result", "result_ids": ["c1", "c2"], "input_ids": []}]
    events = log + b.events
    full_check(events)
    assert all(x == y for x, y in rebuilt_request_hashes(events, b.cfg))


def test_park_with_a_device_call_open_then_its_result_is_late():
    scn = park_scenario(cfg=acfg(DEVICE, grace=0), ops_manual=True, script=[
        tool_calls([("c1", "bash", {"command": "x"}), ("c_dev", "calendar", {})]),
        text("waiting for the calendar"), text("09:30 standup")])
    scn.task("x")
    scn.start()
    scn.wait_for(lambda: scn.ops.submitted)
    scn.ops.complete("c1", "x out")
    scn.wait_event("llm_response", 2)
    assert request(scn, 2).messages[-1] == {
        "role": "tool", "tool_call_id": "c_dev", "content": PLACEHOLDER}
    scn.control("park", {"reason": "idle"})
    scn.wait_event("parked")                   # a device call does not hold the park
    scn.inbound.put(("link", "up"))
    scn.wait_for(lambda: any(b.get("kind") == "resumed" for b in scn.bodies("clock")))
    scn.control("side_result", {"call_id": "c_dev", "side": "device", "content": "09:30", "ok": True})
    assert scn.run()["answer"] == "09:30 standup"
    assert scn.bodies("tool_result")[1] == {"call_id": "c_dev", "content": "09:30", "late": True}
    assert request(scn, 3).messages[-1] == late("c_dev", "09:30", "calendar")
    assert verify_checkpoints(scn.events) == []
    check_async_log(scn)


def test_restore_of_a_parked_async_log_keeps_waiting_then_takes_the_late_result():
    a = park_scenario(cfg=acfg(DEVICE, grace=0), ops_manual=True, script=[
        tool_calls([("c1", "bash", {"command": "x"}), ("c_dev", "calendar", {})]),
        text("waiting")])
    a.task("x")
    a.start()
    a.wait_for(lambda: a.ops.submitted)
    a.ops.complete("c1", "x out")
    a.wait_event("llm_response", 2)
    a.control("park", {"reason": "idle"})
    a.wait_event("parked")
    log = list(a.events)
    b = Scenario(cfg=acfg(DEVICE, grace=0, resume=True), script=[text("fine")])
    b.coord.restore(log)
    b.start()
    b.wait_for(lambda: len(b.events) >= 2)
    assert quiet(b) == ["clock", "clock"]                      # nothing lost, no turn
    b.control("side_result", {"call_id": "c_dev", "side": "device", "content": "ok", "ok": True})
    assert b.run()["answer"] == "fine"
    assert b.bodies("tool_result") == [{"call_id": "c_dev", "content": "ok", "late": True}]
    events = log + b.events
    full_check(events)
    assert verify_checkpoints(events) == []
    assert all(x == y for x, y in rebuilt_request_hashes(events, b.cfg))


def test_question_call_shows_a_placeholder_until_answered():
    scn = Scenario(cfg=acfg(grace=0), ops_manual=True, script=[
        tool_calls([("c1", "bash", {"command": "x"}),
                    ("q1", "question", {"text": "Which day?", "options": ["mon", "tue"]})]),
        text("waiting for your answer"), text("tuesday it is")])
    scn.task("plan")
    scn.start()
    scn.wait_event("question")
    scn.ops.complete("c1", "x out")
    scn.wait_event("llm_response", 2)
    assert request(scn, 2).messages[-1] == {
        "role": "tool", "tool_call_id": "q1", "content": PLACEHOLDER}
    qid = scn.bodies("question")[0]["question_id"]
    scn.control("ui_response", {"input_id": "ui-1", "question_id": qid, "answer": "tue"})
    assert scn.run()["answer"] == "tuesday it is"
    assert request(scn, 3).messages[-1] == late("q1", "tue", "question")
    check_async_log(scn)


# ---------------------------------------------------------------- compaction
def test_compaction_before_a_wake_on_placeholders_replays():
    """c1's big result pushes the wake over the window: a compaction turn,
    then the regular turn on c1's result and c2's placeholder; c2's late
    result compacts again (the placeholder is summarised away) and is shown
    as the late message after the summary."""
    cfg = acfg(grace=0, context_window_tokens=1600, reserve_tokens=100)
    scn = Scenario(cfg=cfg, ops_manual=True, script=[
        sized(TWO_CALLS), sized(text("SUMMARY: c1 printed x's, c2 runs.")),
        sized(text("waiting")), sized(text("SUMMARY 2: still waiting for c2.")), sized(text("done"))])
    scn.task(LONG_TASK)
    scn.start()
    scn.wait_for(lambda: len(scn.ops.submitted) == 2)
    scn.ops.complete("c1", BIG)
    scn.wait_event("llm_response", 3)
    assert [t["body"]["type"] for t in scn.of("turn_start")] == ["regular", "compaction", "regular"]
    after = request(scn, 3)
    assert after.messages[1]["content"].startswith("SUMMARY")
    assert after.messages[-1] == {"role": "tool", "tool_call_id": "c2", "content": PLACEHOLDER}
    c = scn.bodies("compaction")[0]
    assert c["tokens_after"] < c["tokens_before"]
    scn.ops.complete("c2", "two")
    assert scn.run()["answer"] == "done"
    assert [t["body"]["type"] for t in scn.of("turn_start")] == [
        "regular", "compaction", "regular", "compaction", "regular"]
    last = request(scn, 5)
    assert last.messages[1]["content"].startswith("SUMMARY 2")
    assert last.messages[-1] == late("c2", "two")
    check_async_log(scn)


# ---------------------------------------------------------------- e2e
def test_e2e_guest_subprocess_async_placeholder_then_late_result(guest_factory):
    """The real guest (python -m guest.main) with async_tools: a fast and a
    slow bash call; the grace window closes on the fast result (wake{grace},
    the slow call as a placeholder), the model ends its turn to wait, the
    slow result lands late and wakes it again."""
    from guest.config import from_hello
    from tests.test_guest_e2e import check_replay, hello_body

    hello = hello_body(async_tools=True)
    hello["config"]["limits"]["grace_s"] = 0.5      # the fast echo lands well inside it
    g = guest_factory(hello, script=[
        tool_calls([("c_fast", "bash", {"command": "echo fast"}),
                    ("c_slow", "bash", {"command": "sleep 2; echo slow"})]),
        text("The fast one said fast; waiting for the slow one."),
        text("fast, then slow."),
    ])
    g.send_task("run both")
    settle = g.host.wait_for("settle", timeout=20)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "fast, then slow."
    assert [w["reason"] for w in g.of("wake")] == ["grace", "tool_result"]
    assert g.of("wake")[0]["result_ids"] == ["c_fast"]
    results = {r["call_id"]: r for r in g.of("tool_result")}
    assert "late" not in results["c_fast"] and results["c_slow"]["late"] is True
    r1, r2, r3 = g.llm.requests
    assert ASYNC_TOOLS_NOTE in r1["messages"][0]["content"]
    assert r2["messages"][-1] == {"role": "tool", "tool_call_id": "c_slow", "content": PLACEHOLDER}
    assert r3["messages"][: len(r2["messages"])] == r2["messages"]
    assert r3["messages"][-1] == late("c_slow", results["c_slow"]["content"])
    state = check_replay(g.events)
    assert state.placeholders == {"c_slow"} and state.counters["placeholder_fills"] == 1
    cfg = from_hello(hello)
    pairs = rebuilt_request_hashes(g.events, cfg)
    assert len(pairs) == 3 and all(a == b for a, b in pairs)
