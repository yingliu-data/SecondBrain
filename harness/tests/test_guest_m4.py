"""M4 guest: mcp-side calls over :5011 (docs/M4-CONTRACTS.md sections 3-4).

guest.main as a subprocess with FakeHost (:5000, :5001), FakeLLM (:5010) and
FakeGateway (:5011, the forwarder + gateway double): a promoted tool, a call
through the `call_tool` meta tool, an isError result, an op timeout, the
input-required relay (answer, deadline -> cancel, two rounds, park while the
op is suspended), long calls across a park (re-attach, 410 -> lost, expiry
while parked). Every log validates and replays (fold.replay; checkpoints
verify where the guest parked). Plus unit tests of guest.mcp.
"""
from __future__ import annotations

import io
import json
import time

import pytest

from guest import mcp as mcpmod
from guest.coordinator import LOST_CONTENT
from guest.transport import UnixConnector
from tests.fakes.fake_gateway import APPROVAL_SCHEMA, FakeGateway
from tests.fakes.fake_llm import text, tool_calls
from tests.test_guest_e2e import check_replay, hello_body
from tests.test_guest_m3 import check_parked_log, rig, wait_until  # noqa: F401 (fixture)

PIN = "ab" * 32


def mcp_entry(name, timeout_s=30, description=None, properties=None):
    description = description or f"fake {name}"
    return {"name": name, "side": "mcp", "pin": PIN, "timeout_s": timeout_s, "description": description,
            "schema": {"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties or {}}}}}


TOOLS = [
    mcp_entry("wcc-events__draft_event", properties={"title": {"type": "string"},
                                                     "date": {"type": "string"}}),
    mcp_entry("wcc-events__fail"),
    mcp_entry("wcc-events__approve"),
    mcp_entry("search_tools"), mcp_entry("describe_tool"), mcp_entry("call_tool"),
]


def m4_hello(extra=(), *, question_timeout_s=5, **limits):
    h = hello_body([*TOOLS, *extra])
    h["config"]["limits"].update(limits)
    h["config"]["question_timeout_s"] = question_timeout_s
    return h


def run(rig, script, hello=None):
    r = rig(hello or m4_hello(), script=script, gateway=True)
    proc = r.spawn()
    r.task()
    return r, proc


def settle_of(r, proc, timeout=15):
    settle = r.host.wait_for("settle", timeout=timeout)["body"]
    assert r.finish(proc) == 0, r.stderr
    return settle


# ---------------------------------------------------------------- calls
def test_promoted_tool_call_over_the_gateway(rig):
    args = {"title": "October meetup", "date": "2026-10-21"}
    r, proc = run(rig, [tool_calls([("call_mcp_1", "wcc-events__draft_event", args)]),
                        text("Drafted.")])
    settle = settle_of(r, proc)
    assert settle["outcome"] == "done" and settle["answer"] == "Drafted."
    assert r.of("op_state") == [
        {"call_id": "call_mcp_1", "state": "running", "side_ref": "gateway:wcc-events"},
        {**r.of("op_state")[1], "call_id": "call_mcp_1", "state": "done"}]
    assert r.of("tool_result")[0]["content"] == "Drafted October meetup on 2026-10-21"
    req = r.host.gateway.requests[0]
    assert req["body"] == {"jsonrpc": "2.0", "id": "call_mcp_1#0", "method": "tools/call",
                           "params": {"name": "wcc-events__draft_event", "arguments": args}}
    assert req["headers"]["x-sb-call"] == "call_mcp_1" and "x-sb-reattach" not in req["headers"]
    assert req["headers"]["accept"] == "application/json, text/event-stream"
    # no side_result for an mcp call; the schema reached the model; pins recorded
    assert r.host.errors == []
    offered = {t["function"]["name"]: t for t in r.llm.requests[0]["tools"]}
    assert offered["wcc-events__draft_event"]["function"]["parameters"]["properties"] == {
        "title": {"type": "string"}, "date": {"type": "string"}}
    start = r.of("session_start")[0]["tools"]
    assert {"name": "wcc-events__draft_event", "side": "mcp", "pin": PIN} in start
    assert {"name": "call_tool", "side": "mcp", "pin": PIN} in start
    check_replay(r.host.events)


def test_call_tool_meta_and_is_error(rig):
    r, proc = run(rig, [
        tool_calls([("call_meta", "call_tool", {"name": "wcc-events__draft_event",
                                                "arguments": {"title": "T", "date": "D"}}),
                    ("call_fail", "wcc-events__fail", {}),
                    ("call_search", "search_tools", {"query": "draft"})]),
        text("ok")])
    settle_of(r, proc)
    results = {b["call_id"]: b["content"] for b in r.of("tool_result")}
    assert results["call_meta"] == "Drafted T on D"
    assert results["call_fail"] == "error: upstream said no"
    assert json.loads(results["call_search"]) == [{"name": "wcc-events__draft_event",
                                                   "description": "fake wcc-events__draft_event"}]
    states = {(b["call_id"], b["state"]) for b in r.of("op_state")}
    assert ("call_fail", "failed") in states and ("call_meta", "done") in states
    refs = {b["call_id"]: b.get("side_ref") for b in r.of("op_state") if b["state"] == "running"}
    assert refs == {"call_meta": "gateway:wcc-events", "call_fail": "gateway:wcc-events",
                    "call_search": "gateway"}
    check_replay(r.host.events)


def test_op_timeout_from_the_hello_entry_closes_the_call(rig):
    hello = m4_hello([mcp_entry("wcc-events__slow", timeout_s=0.5)])
    r, proc = run(rig, [tool_calls([("call_slow", "wcc-events__slow", {})]), text("It timed out.")],
                  hello)
    t0 = time.monotonic()
    settle = settle_of(r, proc)
    assert time.monotonic() - t0 < 10
    assert settle["answer"] == "It timed out."
    assert [b["state"] for b in r.of("op_state")] == ["running", "timeout"]
    assert r.of("tool_result")[0]["content"] == "error: mcp call wcc-events__slow timed out after 0.5 s"
    check_replay(r.host.events)


def test_gateway_unreachable_is_an_error_result(rig):
    r = rig(m4_hello(), script=[tool_calls([("call_x", "wcc-events__draft_event", {})]),
                                text("No gateway.")])          # no gateway=True: :5011 refuses
    proc = r.spawn()
    r.task()
    settle = settle_of(r, proc)
    assert settle["outcome"] == "done"
    assert r.of("tool_result")[0]["content"].startswith("error: gateway unreachable")
    check_replay(r.host.events)


# ---------------------------------------------------------------- input required
def test_input_required_question_answer_final_result(rig):
    r, proc = run(rig, [tool_calls([("call_ok", "wcc-events__approve", {"rounds": 2})]),
                        text("Approved.")])
    q1 = r.host.wait_for("question", timeout=10)
    assert q1["body"]["call_id"] == "call_ok"
    assert q1["body"]["text"] == "Approve the draft? (round 1)"
    assert q1["body"]["options"] == ["approve", "reject"]
    assert q1["body"]["input_request"]["schema"] == APPROVAL_SCHEMA
    r.host.send("ui_response", {"input_id": "ui-1", "question_id": q1["body"]["question_id"],
                                "answer": "approve"})
    q2 = r.host.wait_for("question", timeout=10, after_seq=q1["seq"])
    r.host.send("ui_response", {"input_id": "ui-2", "question_id": q2["body"]["question_id"],
                                "answer": "Approve"})
    settle = settle_of(r, proc)
    assert settle["answer"] == "Approved."
    assert r.of("tool_result")[0]["content"] == "approved: approve"
    # one operation: running ... done, never a second running
    assert [b["state"] for b in r.of("op_state")] == ["running", "done"]
    reqs = r.host.gateway.requests
    assert [q["body"]["id"] for q in reqs] == ["call_ok#0", "call_ok#1", "call_ok#2"]
    second = reqs[1]["body"]["params"]
    assert second["name"] == "wcc-events__approve" and second["arguments"] == {"rounds": 2}
    assert second["_meta"]["sb/input_response"] == {
        "request_state": q1["body"]["input_request"]["request_state"], "action": "accept",
        "content": {"decision": "approve"}}
    assert all(q["headers"]["x-sb-call"] == "call_ok" and "x-sb-reattach" not in q["headers"]
               for q in reqs)
    check_replay(r.host.events)


def test_question_deadline_sends_cancel(rig):
    r, proc = run(rig, [tool_calls([("call_c", "wcc-events__approve", {})]), text("Cancelled.")],
                  m4_hello(question_timeout_s=1))
    settle = settle_of(r, proc)
    assert settle["answer"] == "Cancelled."
    assert "question_deadline" in [c["kind"] for c in r.of("clock")]
    assert [g for g in r.of("guard_decision") if g["reason"] == "unanswered"]
    reqs = r.host.gateway.requests
    assert reqs[-1]["body"]["params"]["_meta"]["sb/input_response"]["action"] == "cancel"
    assert r.of("tool_result")[0]["content"] == "error: input request canceled by the user"
    assert [b["state"] for b in r.of("op_state")] == ["running", "failed"]
    check_replay(r.host.events)


def test_park_while_input_required_then_answer_after_resume(rig):
    r, proc = run(rig, [tool_calls([("call_p", "wcc-events__approve", {})]), text("Done.")],
                  m4_hello(question_timeout_s=60))
    q = r.host.wait_for("question", timeout=10)
    r.host.send("park", {"reason": "question"})
    r.host.wait_for("parked", timeout=10)        # at once: the suspended op holds nothing
    r.host.wait_disconnected(timeout=5)
    assert len(r.host.gateway.requests) == 1
    r.host.listen()
    r.host.wait_connected(timeout=10, count=2)
    r.host.send("ui_response", {"input_id": "ui-p", "question_id": q["body"]["question_id"],
                                "answer": "approve"})
    settle = settle_of(r, proc)
    assert settle["answer"] == "Done."
    assert r.of("tool_result")[0]["content"] == "approved: approve"
    assert "x-sb-reattach" not in r.host.gateway.requests[-1]["headers"]
    check_parked_log(r.host.events)


# ---------------------------------------------------------------- long calls and park
SLOW = [mcp_entry("wcc-events__slow", timeout_s=30)]


def test_park_with_mcp_call_in_flight_reattaches_after_resume(rig):
    r, proc = run(rig, [tool_calls([("call_long", "wcc-events__slow", {})]), text("Long done.")],
                  m4_hello(SLOW, park_grace_s=5))
    r.host.gateway.wait_requests(1)
    t0 = time.monotonic()
    r.host.send("park", {"reason": "idle"})
    r.host.wait_for("parked", timeout=10)
    assert time.monotonic() - t0 < 3, "an in-flight mcp op must not hold the park"
    r.host.wait_disconnected(timeout=5)
    r.host.gateway.release()                     # the result arrives while the VM is parked
    r.host.gateway.wait_done("call_long")
    r.host.listen()
    settle = settle_of(r, proc)
    assert settle["answer"] == "Long done."
    assert [b["state"] for b in r.of("op_state")] == ["running", "done"]
    assert r.of("tool_result")[0]["content"] == "slow done"
    reqs = r.host.gateway.requests
    assert len(reqs) == 2 and reqs[1]["headers"]["x-sb-reattach"] == "1"
    assert reqs[1]["body"] == reqs[0]["body"]
    assert r.host.gateway.calls == {"call_long": 1}, "a re-attach never re-executes"
    types = r.types()
    assert types.index("parked") < types.index("tool_result")
    check_parked_log(r.host.events)


def test_reattach_answered_410_is_lost(rig):
    r, proc = run(rig, [tool_calls([("call_gone", "wcc-events__slow", {})]), text("Lost it.")],
                  m4_hello(SLOW))
    r.host.gateway.wait_requests(1)
    r.host.send("park", {"reason": "idle"})
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)
    r.host.gateway.forget("call_gone")           # agent-api restarted: the table is gone
    r.host.listen()
    settle = settle_of(r, proc)
    assert settle["answer"] == "Lost it."
    assert [b["state"] for b in r.of("op_state")] == ["running", "lost"]
    assert r.of("tool_result")[0]["content"].startswith("error: operation lost")
    assert r.host.gateway.calls == {"call_gone": 1}
    check_parked_log(r.host.events)


def test_mcp_op_that_expires_while_parked_is_a_timeout_without_reattach(rig):
    r, proc = run(rig, [tool_calls([("call_exp", "wcc-events__slow", {})]), text("Too slow.")],
                  m4_hello([mcp_entry("wcc-events__slow", timeout_s=1.0)]))
    r.host.gateway.wait_requests(1)
    r.host.send("park", {"reason": "idle"})
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)
    time.sleep(1.5)                              # the op timer keeps running across the park
    r.host.listen()
    settle = settle_of(r, proc)
    assert settle["answer"] == "Too slow."
    assert [b["state"] for b in r.of("op_state")] == ["running", "timeout"]
    assert len(r.host.gateway.requests) == 1
    check_parked_log(r.host.events)


def test_fresh_process_after_park_loses_the_in_flight_mcp_call(rig):
    r, proc = run(rig, [tool_calls([("call_dead", "wcc-events__slow", {})]), text("Recovered.")],
                  m4_hello(SLOW))
    r.host.gateway.wait_requests(1)
    r.host.send("park", {"reason": "idle"})
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)
    r.kill(proc)
    r.host.listen()
    proc2 = r.spawn(wait_hello=False)
    r.host.wait_connected(timeout=10, count=r.host.connections + 1)
    hello = r.host.send("hello", dict(r.hello, resume=True))
    r.host.wait_acked_cseq(hello["cseq"], timeout=10)
    settle = settle_of(r, proc2)
    assert settle["answer"] == "Recovered."
    assert [b["state"] for b in r.of("op_state")] == ["running", "lost"]
    assert r.of("tool_result")[0]["content"] == LOST_CONTENT
    assert len(r.host.gateway.requests) == 1     # never re-attached by the new process
    check_parked_log(r.host.events)


def test_fresh_process_after_park_keeps_a_suspended_input_request(rig):
    """The returned log ends with parked and the mcp op is suspended on its
    question: the new process rebuilds it from question.input_request and
    re-sends on the answer (same call, attempt 1)."""
    r, proc = run(rig, [tool_calls([("call_s", "wcc-events__approve", {})]), text("Kept.")],
                  m4_hello(question_timeout_s=60))
    q = r.host.wait_for("question", timeout=10)
    r.host.send("park", {"reason": "question"})
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)
    r.kill(proc)
    r.host.listen()
    proc2 = r.spawn(wait_hello=False)
    r.host.wait_connected(timeout=10, count=r.host.connections + 1)
    hello = r.host.send("hello", dict(r.hello, resume=True))
    r.host.wait_acked_cseq(hello["cseq"], timeout=10)
    r.host.send("ui_response", {"input_id": "ui-s", "question_id": q["body"]["question_id"],
                                "answer": "approve"})
    settle = settle_of(r, proc2)
    assert settle["answer"] == "Kept."
    assert r.of("tool_result")[0]["content"] == "approved: approve"
    assert r.host.gateway.requests[-1]["body"]["id"] == "call_s#1"
    check_parked_log(r.host.events)


# ---------------------------------------------------------------- guest.mcp units
def test_client_against_the_fake_gateway(tmp_path):
    gw = FakeGateway(tmp_path).start()
    try:
        client = mcpmod.McpClient(UnixConnector(tmp_path))
        res = client.call_tool("c1", "wcc-events__draft_event", {"title": "A", "date": "B"})
        assert res == mcpmod.McpResult("Drafted A on B", False, None)
        res = client.call_tool("c2", "wcc-events__approve", {})
        assert res.input_required["schema"] == APPROVAL_SCHEMA
        assert res.input_required["expires_in_s"] == 3600
        with pytest.raises(mcpmod.McpError) as err:
            client.call_tool("c-unknown", "wcc-events__draft_event", {}, reattach=True)
        assert err.value.status == 410
        call = client.start("c3", "wcc-events__slow", {})
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("e", _raises(call.run)))
        t.start()
        gw.wait_requests(4)
        call.close()
        t.join(5)
        assert isinstance(out["e"], mcpmod.McpAborted)
    finally:
        gw.stop()


def _raises(fn):
    try:
        fn()
    except Exception as e:   # noqa: BLE001
        return e
    return None


class _Resp:
    def __init__(self, text):
        self._f = io.BytesIO(text.encode())

    def readline(self):
        return self._f.readline()


def test_sse_body_and_rpc_error():
    call = mcpmod.McpClient(None).start("c", "t", {})
    body = ('event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            'data: {"jsonrpc":"2.0","id":"c#0","result":{"content":[{"type":"text","text":"hi"}]}}\n\n')
    assert mcpmod.parse_call_result(mcpmod._check_rpc(call._from_sse(_Resp(body)))).text == "hi"
    with pytest.raises(mcpmod.McpError, match="without a response"):
        call._from_sse(_Resp("data: {}\n\n"))
    with pytest.raises(mcpmod.McpError, match="-32602"):
        mcpmod._check_rpc({"error": {"code": -32602, "message": "bad"}})


def test_result_text_and_elicitation_mapping():
    r = mcpmod.parse_call_result({"content": [{"type": "text", "text": "a"}, {"type": "image"},
                                              {"type": "resource", "resource": {"text": "r"}}],
                                  "isError": True})
    assert r.text == "a\n[image content]\nr" and r.is_error
    assert mcpmod.parse_call_result({"structuredContent": {"b": 1, "a": 2}}).text == '{"a": 2, "b": 1}'
    with pytest.raises(mcpmod.McpError):
        mcpmod.parse_call_result({"_meta": {"sb/input_required": {"message": "x"}}})
    assert mcpmod.rpc_id("call_1", 2) == "call_1#2"
    assert mcpmod.question_options(APPROVAL_SCHEMA) == ["approve", "reject"]
    assert mcpmod.question_options({"type": "object", "properties": {"n": {"type": "string"}}}) == []
    boolean = {"properties": {"ok": {"type": "boolean"}}}
    assert mcpmod.answer_content(boolean, "Yes") == {"ok": True}
    assert mcpmod.answer_content({"properties": {"n": {"type": "integer"}}}, "3") == {"n": 3}
    two = {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["b"]}
    assert mcpmod.answer_content(two, '{"a": "x", "b": "y"}') == {"a": "x", "b": "y"}
    assert mcpmod.answer_content(two, "free") == {"b": "free"}
    assert mcpmod.answer_content({}, "anything") == {}
    assert mcpmod.input_response("rs", "decline", boolean) == {"request_state": "rs", "action": "decline"}
    assert mcpmod.input_response("rs", "no", boolean) == {
        "request_state": "rs", "action": "accept", "content": {"ok": False}}
