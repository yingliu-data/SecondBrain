"""stream.py produces the (retired) legacy loop's SSE strings byte for byte."""
import json

import pytest

from app.host.stream import DONE, KEEPALIVE, Projector, project, sse, tokens
from host_fakes import HASH, token_text

SID = "s1"


def ev(seq, type, body):
    return {"session_id": SID, "seq": seq, "ts": "t", "schema": 1, "type": type, "body": body}


def test_final_text_matches_legacy_word_wise_tokens():
    # what app/agent/loop.py streamed for a final answer (deleted in M6)
    legacy = [f"event: token\ndata: {json.dumps({'text': w + ' '})}\n\n"
              for w in "Hello there, wörld 1 2".split(" ")] + ["event: done\ndata: {}\n\n"]
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
    # the settle adds no done: the worker's finish_input ends the stream
    assert out + [DONE] == legacy
    assert legacy[-1] == DONE == "event: done\ndata: {}\n\n"
    assert out[0] == "event: token\ndata: " + json.dumps({"text": "Hello "}) + "\n\n"


def test_settle_streams_answer_when_no_text_was_streamed():
    p = Projector()
    p.project(ev(2, "input", {"input_id": "i", "kind": "task", "text": "x", "dedupe": "new"}))
    out = p.project(ev(3, "settle", {"input_id": "i", "outcome": "done", "answer": "a b",
                                     "usage": {}, "manifest": {}}))
    assert out == tokens("a b")
    # a stateless project() of a settle is just its answer (finish_input ends the stream)
    assert project(ev(3, "settle", {"input_id": "i", "outcome": "done", "answer": "a b",
                                    "usage": {}, "manifest": {}})) == tokens("a b")


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
    # a non-streamed turn's whole-message delta goes out word-wise (the legacy shape)
    assert p.live({"type": "delta", "body": {"turn_id": "t9", "kind": "text", "index": 0,
                                             "text": "Hello"}}) == tokens("Hello")
    assert p.live({"type": "log", "body": {"msg": "x"}}) == []
    # a turn that sent its text live is not re-emitted at llm_response
    assert p.project(ev(6, "llm_response", {"turn_id": "t9", "finish_reason": "stop", "usage": {},
                                            "message": {"role": "assistant",
                                                        "content": "Hello"}})) == []
    assert p.project(ev(7, "settle", {"input_id": "i", "outcome": "done", "answer": "Hello",
                                      "usage": {}, "manifest": {}})) == []
    assert KEEPALIVE == ": keepalive\n\n"


def delta(turn_id, kind, text):
    return {"type": "delta", "body": {"session_id": SID, "turn_id": turn_id, "kind": kind,
                                      "index": 0, "text": text}}


def turn_start(seq, turn_id):
    return ev(seq, "turn_start", {"turn_id": turn_id, "type": "regular",
                                  "includes_through_seq": seq - 1, "model": "m", "params": {},
                                  "request_hash": HASH, "preamble_hash": HASH,
                                  "workspace_tree_id": None})


def response(seq, turn_id, content):
    return ev(seq, "llm_response", {"turn_id": turn_id, "finish_reason": "stop", "usage": {},
                                    "message": {"role": "assistant", "content": content}})


def settle(seq, answer):
    return ev(seq, "settle", {"input_id": "i", "outcome": "done", "answer": answer,
                              "usage": {}, "manifest": {}})


def test_streamed_content_deltas_are_tokens_verbatim_and_suppress_the_response():
    """M2 (D17): one token per fragment, boundaries as the model made them;
    reasoning and tool_call fragments never reach the client; the turn's
    llm_response and the settle add no text."""
    p = Projector()
    p.begin_task()
    out = []
    out += p.live(delta("t1", "reasoning", "thinking..."))
    assert out == []
    out += p.live(delta("t1", "content", "Hel"))
    out += p.live(delta("t1", "content", "lo, wör"))
    out += p.live(delta("t1", "tool_call", '{"q":'))
    out += p.live(delta("t1", "content", "ld\n"))
    out += p.live(delta("t1", "content", ""))               # empty fragments are dropped
    assert out == [sse("token", {"text": "Hel"}), sse("token", {"text": "lo, wör"}),
                   sse("token", {"text": "ld\n"})]
    # the input event is stored after the first deltas (live messages overtake
    # the guest's log writer) and must not reset the "streamed" flag
    out += p.project(ev(2, "input", {"input_id": "i", "kind": "task", "text": "x",
                                     "dedupe": "new"}))
    out += p.project(response(5, "t1", "Hello, wörld"))
    out += p.project(settle(6, "Hello, wörld"))
    assert token_text(out) == "Hello, wörld\n" and DONE not in out
    assert sum(1 for s in out if s.startswith("event: token")) == 3


def test_streamed_tool_turn_preamble_ends_with_a_newline():
    """A turn that says "Let me check." and then calls a tool streamed its text
    live; one newline goes out before the next turn's text so the answer does
    not run into it ("Let me check.\nDone." not "Let me check.Done.")."""
    p = Projector()
    p.begin_task()
    out = p.project(turn_start(2, "t1"))
    out += p.live(delta("t1", "content", "Let me check."))
    tool_turn = response(3, "t1", "Let me check.")
    tool_turn["body"]["finish_reason"] = "tool_calls"
    tool_turn["body"]["message"]["tool_calls"] = [{"id": "c1", "type": "function",
        "function": {"name": "bash", "arguments": "{}"}}]
    out += p.project(tool_turn)
    out += p.project(turn_start(7, "t_mid"))       # a tool turn without text adds nothing
    out += p.project(turn_start(8, "t2"))
    out += p.live(delta("t2", "content", "Done."))
    out += p.project(response(9, "t2", "Done."))
    out += p.project(settle(10, "Done."))
    assert out == [sse("token", {"text": "Let me check."}), sse("token", {"text": "\n"}),
                   sse("token", {"text": "Done."})]


def test_follow_up_turn_text_is_separated_by_a_newline():
    """"Done." streamed, then a follow-up wakes the guest and the next turn
    streams "Next.": the client reads "Done.\nNext."; text that already ends
    in whitespace (the word-wise shape) gets no separator."""
    p = Projector()
    p.begin_task()
    out = p.project(turn_start(3, "t1"))
    out += p.live(delta("t1", "content", "Done."))
    out += p.project(response(4, "t1", "Done."))
    out += p.project(ev(5, "input", {"input_id": "i2", "kind": "follow_up", "text": "and?",
                                     "dedupe": "new"}))
    out += p.project(ev(6, "wake", {"reason": "input", "result_ids": [], "input_ids": ["i2"]}))
    out += p.project(turn_start(7, "t2"))
    out += p.live(delta("t2", "content", "Next."))
    out += p.project(response(8, "t2", "Next."))
    out += p.project(settle(9, "Next."))
    assert token_text(out) == "Done.\nNext." and DONE not in out
    p.begin_task()
    out = p.live(delta("t3", "text", "One two"))
    out += p.live(delta("t4", "text", "Three"))
    assert out == tokens("One two") + tokens("Three")


def test_non_streamed_turn_falls_back_to_word_wise_response():
    """LLM_STREAM=false (or a guest without deltas): the llm_response text
    goes out word-wise, as the legacy loop did; settle adds nothing."""
    p = Projector()
    p.begin_task()
    out = p.project(ev(2, "input", {"input_id": "i", "kind": "task", "text": "x", "dedupe": "new"}))
    out += p.project(response(5, "t1", "Hello there world"))
    assert out[1:] == tokens("Hello there world")
    out += p.project(settle(6, "Hello there world"))
    assert DONE not in out and token_text(out) == "Hello there world "


def test_begin_task_resets_between_tasks_and_tool_turns_add_no_text():
    p = Projector()
    p.begin_task()
    p.live(delta("t1", "content", "first"))
    assert p.project(settle(3, "first")) == []
    # a second task on the same projector: nothing streamed yet
    p.begin_task()
    assert p.project(settle(6, "second")) == tokens("second")
    # a tool-call turn: no text at llm_response, deltas (if any) still stream
    p.begin_task()
    assert p.project(ev(8, "llm_response", {
        "turn_id": "t3", "finish_reason": "tool_calls", "usage": {},
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
    })) == []
    assert p.live(delta("t4", "content", "done")) == [sse("token", {"text": "done"})]
    assert p.project(settle(12, "done")) == []


def test_compaction_summary_is_never_shown():
    """A compaction turn's llm_response is context for the model, not answer
    text: it adds no token and does not suppress the settle answer."""
    p = Projector()
    p.begin_task()
    comp = turn_start(2, "tc")
    comp["body"]["type"] = "compaction"
    assert p.project(comp) == []
    assert p.project(response(3, "tc", "goal: ... progress: ...")) == []
    out = p.project(settle(4, "Hello"))
    assert out == tokens("Hello")
