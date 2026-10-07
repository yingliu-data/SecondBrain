"""stream.py produces the legacy loop's SSE strings byte for byte."""
import json

import pytest

from app.host.stream import DONE, KEEPALIVE, Projector, project, sse, tokens
from host_fakes import HASH
from test_sessions_and_loop import FakeLLM, FakeRegistry, collect

SID = "s1"


def ev(seq, type, body):
    return {"session_id": SID, "seq": seq, "ts": "t", "schema": 1, "type": type, "body": body}


@pytest.mark.asyncio
async def test_final_text_matches_legacy_word_wise_tokens():
    from app.agent.loop import run_agent_loop

    class LLM(FakeLLM):
        async def chat_completion(self, messages, tools=None, max_tokens=None,
                                  chat_template_kwargs=None):
            return {"choices": [{"message": {"content": "Hello there, wörld 1 2"},
                                 "finish_reason": "stop"}]}

    legacy = await collect(run_agent_loop("hi", [], FakeRegistry(), LLM()))
    p = Projector()
    out = p.project(ev(2, "input", {"input_id": "in_1", "kind": "task", "text": "hi",
                                    "dedupe": "new"}))
    assert out == ['event: seq\ndata: {"input_id": "in_1", "seq": 2}\n\n']
    out = p.project(ev(5, "llm_response", {"turn_id": "t", "finish_reason": "stop", "usage": {},
                                           "message": {"role": "assistant",
                                                       "content": "Hello there, wörld 1 2"}}))
    out += p.project(ev(6, "settle", {"input_id": "in_1", "outcome": "done",
                                      "answer": "Hello there, wörld 1 2", "usage": {},
                                      "manifest": {}}))
    assert out == legacy
    assert out[-1] == DONE == "event: done\ndata: {}\n\n"
    assert out[0] == "event: token\ndata: " + json.dumps({"text": "Hello "}) + "\n\n"


def test_settle_streams_answer_when_no_text_was_streamed():
    p = Projector()
    p.project(ev(2, "input", {"input_id": "i", "kind": "task", "text": "x", "dedupe": "new"}))
    out = p.project(ev(3, "settle", {"input_id": "i", "outcome": "done", "answer": "a b",
                                     "usage": {}, "manifest": {}}))
    assert out == tokens("a b") + [DONE]
    # a stateless project() of a settle is just done
    assert project(ev(3, "settle", {"input_id": "i", "outcome": "done", "answer": "a b",
                                    "usage": {}, "manifest": {}}))[-1] == DONE


def test_tool_call_device_and_avatar_result():
    p = Projector()
    call = ev(7, "tool_call", {"turn_id": "t", "call_id": "call_1", "name": "get_calendar_events",
                               "side": "device", "args": {"day": "today"}, "state": "RUNNING"})
    assert p.project(call) == [
        "event: tool_call\ndata: " + json.dumps(
            {"id": "call_1", "name": "get_calendar_events", "arguments": {"day": "today"}}) + "\n\n"]
    host = ev(8, "tool_call", {"turn_id": "t", "call_id": "call_2", "name": "set_pose",
                               "side": "host", "args": {}, "state": "RUNNING"})
    assert p.project(host) == []
    res = ev(9, "tool_result", {"call_id": "call_2", "content": json.dumps({"pose": "wave"})})
    assert p.project(res) == [
        "event: avatar_command\ndata: " + json.dumps({"name": "set_pose", "result": {"pose": "wave"}})
        + "\n\n"]
    # a host result the projector was told about verbatim wins over the compacted content
    p.project(ev(10, "tool_call", {"turn_id": "t", "call_id": "call_3", "name": "plan_movement",
                                   "side": "host", "args": {}, "state": "RUNNING"}))
    p.note_host_result("call_3", json.dumps({"frames": [1, 2], "loop": False}))
    out = p.project(ev(11, "tool_result", {"call_id": "call_3",
                                           "content": json.dumps({"status": "ok"})}))
    assert json.loads(out[0].split("data: ", 1)[1])["result"]["frames"] == [1, 2]
    # non-avatar and non-JSON results emit nothing
    assert p.project(ev(12, "tool_result", {"call_id": "call_1", "content": "x"})) == []


def test_question_error_and_live_delta():
    p = Projector()
    q = ev(3, "question", {"question_id": "q1", "call_id": "c", "text": "Which?",
                           "options": ["a", "b"], "deadline": 5})
    assert p.project(q) == [sse("question", {"question_id": "q1", "call_id": "c", "text": "Which?",
                                             "options": ["a", "b"], "deadline": 5})]
    assert p.project(ev(4, "error", {"class": "transient", "message": "m", "retry": True})) == []
    assert p.project(ev(5, "error", {"class": "fatal", "message": "boom", "retry": False})) == [
        sse("error", {"class": "fatal", "message": "boom"})]
    assert p.live({"type": "delta", "body": {"turn_id": "t9", "kind": "text", "text": "Hel"}}) == [
        sse("token", {"text": "Hel"})]
    assert p.live({"type": "log", "body": {"msg": "x"}}) == []
    # a turn that streamed deltas is not re-emitted word-wise at llm_response
    assert p.project(ev(6, "llm_response", {"turn_id": "t9", "finish_reason": "stop", "usage": {},
                                            "message": {"role": "assistant",
                                                        "content": "Hello"}})) == []
    assert p.project(ev(7, "settle", {"input_id": "i", "outcome": "done", "answer": "Hello",
                                      "usage": {}, "manifest": {}})) == [DONE]
    assert KEEPALIVE == ": keepalive\n\n"
