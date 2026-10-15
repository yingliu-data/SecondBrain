"""guest.provider against the scripted FakeLLM on a Unix socket: the
non-streamed path, the streamed path (SSE parsing and assembly), the retry
de-dup (DeltaRelay), and protocol.sse, the grammar and chunker both use."""
from __future__ import annotations

import json

import pytest

from guest.context import Request
from guest.provider import DIVERGENCE_MARK, DeltaRelay, Provider, ProviderError, assemble_chunks
from protocol.ports import LLM
from protocol.sse import (
    DONE_FRAME, iter_sse_data, response_chunks, split_arguments, split_words, sse_frame,
)
from tests.fakes.fake_llm import FakeLLM, UnixConnector, response, text, tool_calls, write_sse


def make_request(messages=None, tools=None, params=None) -> Request:
    messages = messages or [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    tools = tools if tools is not None else []
    params = params if params is not None else {"temperature": 0, "max_tokens": 64}
    return Request(messages=messages, tools=tools, params=params, system_hash="s" * 64,
                   request_hash="r" * 64, preamble_hash="p" * 64)


@pytest.fixture
def llm(tmp_path):
    fake = FakeLLM(str(tmp_path))
    fake.start()
    yield fake
    fake.stop()


def provider(tmp_path, **kw) -> Provider:
    kw.setdefault("sleep", lambda s: None)
    return Provider(UnixConnector(str(tmp_path)), port=LLM, model="fake-model", timeout_s=5, **kw)


def test_chat_completion_posts_the_vllm_shape_and_returns_the_response(tmp_path, llm):
    llm.push(text("2 + 2 = 4."))
    p = provider(tmp_path)
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    resp = p.chat_completion(make_request(tools=tools))
    assert resp["choices"][0]["message"]["content"] == "2 + 2 = 4."
    assert len(llm.requests) == 1
    body = llm.requests[0]
    assert body["model"] == "fake-model"
    assert body["stream"] is False
    assert body["temperature"] == 0 and body["max_tokens"] == 64
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_no_tools_means_no_tool_choice(tmp_path, llm):
    llm.push(text("ok"))
    provider(tmp_path).chat_completion(make_request(tools=[]))
    body = llm.requests[0]
    assert "tools" not in body and "tool_choice" not in body


def test_usage_is_recorded_and_summed(tmp_path, llm):
    llm.push(text("a", usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
             text("b", usage={"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}))
    p = provider(tmp_path)
    p.chat_completion(make_request())
    assert p.last_usage == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    p.chat_completion(make_request())
    assert p.usage == {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}


def test_tool_call_response_is_returned_verbatim(tmp_path, llm):
    llm.push(tool_calls([("call_1", "bash", {"command": "echo hi"})]))
    resp = provider(tmp_path).chat_completion(make_request())
    msg = resp["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"] == {"name": "bash", "arguments": '{"command":"echo hi"}'}
    assert resp["choices"][0]["finish_reason"] == "tool_calls"


def test_retries_502_then_succeeds_with_backoff(tmp_path, llm):
    llm.push({"status": 502}, {"status": 503}, text("after retries"))
    slept = []
    p = provider(tmp_path, sleep=slept.append)
    resp = p.chat_completion(make_request())
    assert resp["choices"][0]["message"]["content"] == "after retries"
    assert slept == [1.0, 2.0]
    assert p.attempts == 3
    assert len(llm.requests) == 3


def test_gives_up_after_retries_with_a_transient_error(tmp_path, llm):
    llm.push({"status": 504}, {"status": 504}, {"status": 504}, text("never"))
    slept = []
    p = provider(tmp_path, sleep=slept.append, retries=3)
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    assert info.value.transient is True and info.value.status == 504
    assert slept == [1.0, 2.0]
    assert llm.remaining == 1


def test_non_transient_status_is_not_retried(tmp_path, llm):
    llm.push({"status": 400}, text("never"))
    p = provider(tmp_path)
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    assert info.value.transient is False and info.value.status == 400
    assert p.attempts == 1


def test_connection_error_is_transient(tmp_path):
    # no server listening on the socket path
    slept = []
    p = provider(tmp_path, sleep=slept.append, retries=2)
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    assert info.value.transient is True and info.value.status is None
    assert slept == [1.0]


def test_backoff_table_caps_at_last_entry(tmp_path, llm):
    llm.push(*([{"status": 502}] * 4), text("ok"))
    slept = []
    p = provider(tmp_path, sleep=slept.append, retries=5)
    p.chat_completion(make_request())
    assert slept == [1.0, 2.0, 4.0, 4.0]


def test_script_callable_sees_the_request(tmp_path, llm):
    seen = {}

    def entry(request):
        seen.update(request)
        return text("echo: " + request["messages"][-1]["content"])

    llm.push(entry)
    resp = provider(tmp_path).chat_completion(make_request())
    assert resp["choices"][0]["message"]["content"] == "echo: hi"
    assert seen["model"] == "fake-model"


def test_exhausted_script_is_a_non_transient_error(tmp_path, llm):
    with pytest.raises(ProviderError) as info:
        provider(tmp_path).chat_completion(make_request())
    assert info.value.status == 500 and info.value.transient is False


def test_extra_params_pass_through(tmp_path, llm):
    llm.push(text("ok"))
    provider(tmp_path).chat_completion(make_request(params={"temperature": 0.2, "max_tokens": 8, "top_p": 0.9}))
    body = llm.requests[0]
    assert body["top_p"] == 0.9 and body["temperature"] == 0.2 and body["max_tokens"] == 8
    json.dumps(body)   # the recorded body is plain JSON


# ---------------------------------------------------------------- streaming
def streaming(tmp_path, **kw):
    """A provider whose chat_completion streams unless told otherwise."""
    p = provider(tmp_path, **kw)
    call = p.chat_completion
    p.chat_completion = lambda request, *, stream=True, on_delta=None: call(
        request, stream=stream, on_delta=on_delta)
    return p


def strip_ids(resp: dict) -> dict:
    return {k: v for k, v in resp.items() if k in ("choices", "usage", "model")}


def test_stream_posts_stream_true_with_usage_option(tmp_path, llm):
    llm.push(text("ok"))
    streaming(tmp_path).chat_completion(make_request())
    body = llm.requests[0]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "fake-model" and body["max_tokens"] == 64


def test_chat_completion_defaults_to_non_streamed(tmp_path, llm):
    llm.push(text("a"), text("b"))
    p = Provider(UnixConnector(str(tmp_path)), model="fake-model", timeout_s=5, sleep=lambda s: None)
    p.chat_completion(make_request())                 # the default
    p.chat_completion(make_request(), stream=True)
    assert [r["stream"] for r in llm.requests] == [False, True]
    assert "stream_options" not in llm.requests[0]
    with pytest.raises(TypeError):                    # no constructor default any more
        Provider(UnixConnector(str(tmp_path)), stream=True)


def test_streamed_text_assembles_to_the_non_streamed_response(tmp_path, llm):
    script = text("The answer is 4.  Really.", response_id="same")
    llm.push(script, script)
    p = streaming(tmp_path)
    plain = p.chat_completion(make_request(), stream=False)
    streamed = p.chat_completion(make_request(), stream=True)
    assert streamed["choices"] == plain["choices"]
    assert streamed["usage"] == plain["usage"]
    assert streamed["id"] == "same" and streamed["model"] == "fake-model"
    assert streamed["object"] == "chat.completion"
    assert streamed["choices"][0]["finish_reason"] == "stop"
    # the fake really streamed: several fragments and a usage-only chunk
    assert len(llm.streamed) == 1 and llm.streamed[0].count("data: ") >= 6
    assert llm.streamed[0].endswith("data: [DONE]\n\n")


def test_streamed_tool_calls_assemble_to_the_non_streamed_response(tmp_path, llm):
    calls = [("call_1", "bash", {"command": "echo hi there"}), ("call_2", "read", {"path": "/etc/hosts"})]
    script = tool_calls(calls, response_id="tc")
    llm.push(script, script)
    p = streaming(tmp_path)
    plain = p.chat_completion(make_request(), stream=False)
    streamed = p.chat_completion(make_request(), stream=True)
    assert strip_ids(streamed) == strip_ids(plain)
    msg = streamed["choices"][0]["message"]
    assert msg["content"] is None
    assert [c["id"] for c in msg["tool_calls"]] == ["call_1", "call_2"]
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"command": "echo hi there"}
    assert json.loads(msg["tool_calls"][1]["function"]["arguments"]) == {"path": "/etc/hosts"}
    assert streamed["choices"][0]["finish_reason"] == "tool_calls"


def test_on_delta_sequence_for_content(tmp_path, llm):
    llm.push(text("one two  three"))
    deltas = []
    resp = streaming(tmp_path).chat_completion(make_request(), on_delta=deltas.append)
    assert deltas == [{"kind": "content", "index": 0, "text": "one "},
                      {"kind": "content", "index": 0, "text": "two  "},
                      {"kind": "content", "index": 0, "text": "three"}]
    assert "".join(d["text"] for d in deltas) == resp["choices"][0]["message"]["content"]


def test_on_delta_for_multiple_tool_calls_carries_the_index(tmp_path, llm):
    llm.push(tool_calls([("c1", "bash", {"command": "ls -la /tmp"}), ("c2", "glob", {"pattern": "**/*.py"})]))
    deltas = []
    resp = streaming(tmp_path).chat_completion(make_request(), on_delta=deltas.append)
    assert {d["kind"] for d in deltas} == {"tool_call"}
    by_index = {}
    for d in deltas:
        by_index.setdefault(d["index"], []).append(d["text"])
    assert set(by_index) == {0, 1}
    assert 2 <= len(by_index[0]) <= 3 and 2 <= len(by_index[1]) <= 3    # argument fragments
    calls = resp["choices"][0]["message"]["tool_calls"]
    assert "".join(by_index[0]) == calls[0]["function"]["arguments"]
    assert "".join(by_index[1]) == calls[1]["function"]["arguments"]
    assert calls[0]["function"]["name"] == "bash" and calls[1]["function"]["name"] == "glob"


def test_reasoning_content_is_assembled_and_streamed(tmp_path, llm):
    full = text("final answer")
    full["choices"][0]["message"]["reasoning_content"] = "let me think"
    llm.push(full)
    deltas = []
    resp = streaming(tmp_path).chat_completion(make_request(), on_delta=deltas.append)
    msg = resp["choices"][0]["message"]
    assert msg["reasoning_content"] == "let me think" and msg["content"] == "final answer"
    kinds = [d["kind"] for d in deltas]
    assert kinds == ["reasoning"] * 3 + ["content"] * 2
    assert "".join(d["text"] for d in deltas if d["kind"] == "reasoning") == "let me think"


def test_non_streamed_path_delivers_one_text_delta(tmp_path, llm):
    llm.push(text("plain answer here"), tool_calls([("c1", "bash", {"command": "ls"})]))
    deltas = []
    p = provider(tmp_path)
    p.chat_completion(make_request(), on_delta=deltas.append)
    assert deltas == [{"kind": "text", "index": 0, "text": "plain answer here"}]
    p.chat_completion(make_request(), on_delta=deltas.append)     # no content: no delta
    assert len(deltas) == 1


def test_non_streamed_text_delta_comes_once_after_retries(tmp_path, llm):
    llm.push({"status": 502}, {"status": 503}, text("third time"))
    deltas = []
    provider(tmp_path).chat_completion(make_request(), on_delta=deltas.append)
    assert deltas == [{"kind": "text", "index": 0, "text": "third time"}]


def test_streamed_usage_is_recorded_and_summed(tmp_path, llm):
    llm.push(text("a", usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
             text("b", usage={"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}))
    p = streaming(tmp_path)
    p.chat_completion(make_request())
    assert p.last_usage == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    p.chat_completion(make_request())
    assert p.usage == {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}


def test_cut_stream_is_transient_and_retried(tmp_path, llm):
    llm.push({"cut_after": 3, "content": "this stream stops half way through the answer"}, text("after the cut"))
    slept, deltas = [], []
    p = streaming(tmp_path, sleep=slept.append)
    resp = p.chat_completion(make_request(), on_delta=deltas.append)
    assert resp["choices"][0]["message"]["content"] == "after the cut"
    assert slept == [1.0] and p.attempts == 2 and len(llm.requests) == 2
    # the cut attempt's fragments went out live; the retry diverged, so it is
    # marked once and then continues fragment by fragment
    assert [d["text"] for d in deltas] == ["this ", "stream ", DIVERGENCE_MARK + "after ", "the ", "cut"]
    assert "[DONE]" not in llm.streamed[0] and "[DONE]" in llm.streamed[1]


def test_retried_stream_with_the_same_text_is_delivered_once(tmp_path, llm):
    answer = "one two three four five"
    llm.push({"cut_after": 3, "content": answer}, {"cut_after": 5, "content": answer}, text(answer))
    deltas = []
    p = streaming(tmp_path)
    resp = p.chat_completion(make_request(), on_delta=deltas.append)
    assert p.attempts == 3
    assert [d["text"] for d in deltas] == ["one ", "two ", "three ", "four ", "five"]
    assert "".join(d["text"] for d in deltas) == resp["choices"][0]["message"]["content"]


def test_retried_tool_call_stream_is_delivered_once_per_index(tmp_path, llm):
    calls = [("c1", "bash", {"command": "ls -la /tmp"}), ("c2", "glob", {"pattern": "**/*.py"})]
    llm.push({"cut_after": 5, "tool_calls": calls}, tool_calls(calls))
    deltas = []
    resp = streaming(tmp_path).chat_completion(make_request(), on_delta=deltas.append)
    by_index = {}
    for d in deltas:
        assert d["kind"] == "tool_call"
        by_index[d["index"]] = by_index.get(d["index"], "") + d["text"]
    got = resp["choices"][0]["message"]["tool_calls"]
    assert by_index == {0: got[0]["function"]["arguments"], 1: got[1]["function"]["arguments"]}


def test_cut_stream_on_every_attempt_raises_transient(tmp_path, llm):
    llm.push({"cut_after": 1}, {"cut_after": 1}, {"cut_after": 1}, text("never"))
    slept = []
    p = streaming(tmp_path, sleep=slept.append, retries=3)
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    assert info.value.transient is True
    assert slept == [1.0, 2.0] and p.attempts == 3 and llm.remaining == 1


def test_402_is_a_budget_error_and_not_retried(tmp_path, llm):
    llm.push({"status": 402}, text("never"))
    p = streaming(tmp_path)
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    err = info.value
    assert err.status == 402 and err.budget is True and err.overflow is False
    assert err.transient is False and p.attempts == 1
    assert "budget_exhausted" in err.message


def test_400_context_length_is_an_overflow_error(tmp_path, llm):
    llm.push({"status": 400, "message": "This model's maximum context length is 32768 tokens"},
             {"status": 400, "message": "Requested 40000 tokens exceed the context length"},
             {"status": 400, "message": "too many tokens in the prompt"},
             {"status": 400, "message": "invalid value for temperature"})
    p = streaming(tmp_path)
    flags = []
    for _ in range(4):
        with pytest.raises(ProviderError) as info:
            p.chat_completion(make_request())
        flags.append((info.value.status, info.value.overflow, info.value.transient, info.value.budget))
    assert flags == [(400, True, False, False)] * 3 + [(400, False, False, False)]
    assert p.attempts == 4


def test_overflow_is_classified_on_the_non_streamed_path_too(tmp_path, llm):
    llm.push({"status": 400, "message": "maximum context length exceeded"}, {"status": 402})
    p = provider(tmp_path)
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    assert info.value.overflow is True
    with pytest.raises(ProviderError) as info:
        p.chat_completion(make_request())
    assert info.value.budget is True


def test_transient_status_before_the_stream_is_retried(tmp_path, llm):
    llm.push({"status": 503}, text("after 503"))
    slept = []
    p = streaming(tmp_path, sleep=slept.append)
    assert p.chat_completion(make_request())["choices"][0]["message"]["content"] == "after 503"
    assert slept == [1.0]


def test_keep_alive_comments_and_multi_line_events_are_tolerated(tmp_path, llm):
    llm.push({"keepalive": True, "multiline": True, "content": "still here"})
    deltas = []
    p = streaming(tmp_path)
    resp = p.chat_completion(make_request(), on_delta=deltas.append)
    assert resp["choices"][0]["message"]["content"] == "still here"
    assert p.attempts == 1
    assert llm.streamed[0].startswith(": keep-alive\n\n")
    assert "\ndata: " in llm.streamed[0].split("\n\n")[1]       # the two-line event was really sent
    assert [d["text"] for d in deltas] == ["still ", "here"]


def test_iter_sse_data_parses_events_comments_and_done():
    raw = (": keep-alive\r\n\r\n"
           "event: message\r\n"
           "id: 7\r\n"
           "retry: 100\r\n"
           "data: {\"a\":\r\n"
           "data:  1}\r\n"           # data without a space, and with two (one dropped)
           "\r\n"
           "data:{\"b\":2}\n\n"
           "\n\n"                     # an empty event is skipped
           "data: [DONE]\n\n"
           "data: {\"never\":3}\n\n")
    out = list(iter_sse_data(raw.splitlines(keepends=True)))
    assert out == ['{"a":\n 1}', '{"b":2}']
    assert json.loads(out[0]) == {"a": 1}
    # an unterminated final event is flushed at EOF; a bare `data` field is ""
    assert list(iter_sse_data(['data: {"x":1}'])) == ['{"x":1}']
    assert list(iter_sse_data(["data\n", "data: z\n", "\n"])) == ["\nz"]


def test_sse_framing_and_splitters():
    assert sse_frame('{"a":1}') == b'data: {"a":1}\n\n'
    assert DONE_FRAME == b"data: [DONE]\n\n"
    assert list(iter_sse_data((sse_frame("x") + DONE_FRAME + sse_frame("y")).decode().splitlines(True))) == ["x"]
    assert split_words("") == [] and split_words("one two  three") == ["one ", "two  ", "three"]
    assert split_words("  lead") == ["  ", "lead"]
    assert split_arguments("") == [""] and split_arguments("a") == ["a"]
    assert split_arguments("abcd") == ["ab", "cd"]
    assert split_arguments('{"command":"x"}') == ['{"com', 'mand"', ':"x"}']
    assert split_arguments("abcdefghij", parts=4) == ["ab", "cde", "fg", "hij"]
    assert split_arguments("abcdefghij", parts=1) == ["abcdefghij"]
    for n in range(12):
        assert "".join(split_arguments("x" * n)) == "x" * n


def test_response_chunks_shape():
    full = response(content="hi there", tool_calls=[("c1", "bash", {"command": "x"})])
    full["choices"][0]["message"]["reasoning_content"] = "hmm"
    chunks = response_chunks(full)
    deltas = [c["choices"][0]["delta"] if c["choices"] else None for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert deltas[1] == {"reasoning_content": "hmm"}
    assert deltas[2:4] == [{"content": "hi "}, {"content": "there"}]
    first = deltas[4]["tool_calls"][0]
    assert set(first) == {"index", "id", "type", "function"} and set(first["function"]) == {"name", "arguments"}
    assert all(set(d["tool_calls"][0]) == {"index", "function"} for d in deltas[5:-2])
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"] == full["usage"]
    assert all(c["object"] == "chat.completion.chunk" and c["id"] == full["id"] for c in chunks)


def test_write_sse_frames_cut_and_done():
    import io
    chunks = response_chunks(text("a b"))
    buf = io.BytesIO()
    recorded = []
    sent = write_sse(buf, chunks, keepalive=True, on_text=recorded.append)
    assert recorded == [sent] and sent.endswith(DONE_FRAME.decode())
    assert sent.count(": keep-alive\n\n") == len(chunks)
    assert buf.getvalue().endswith(b"0\r\n\r\n")
    payloads = [json.loads(x) for x in iter_sse_data(sent.splitlines(True))]
    assert payloads == json.loads(json.dumps(chunks))
    buf = io.BytesIO()
    cut = write_sse(buf, chunks, cut_after=2, multiline=True)
    assert not cut.endswith(DONE_FRAME.decode()) and not buf.getvalue().endswith(b"0\r\n\r\n")
    assert [json.loads(x) for x in iter_sse_data(cut.splitlines(True))] == json.loads(json.dumps(chunks[:2]))


def test_assemble_chunks_takes_finish_reason_from_the_last_choice_chunk():
    # vLLM style: finish_reason rides on the last content chunk, then usage
    chunks = [
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "length"}]},
        {"id": "c", "model": "m", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    ]
    resp = assemble_chunks(chunks)
    assert resp["choices"][0] == {"index": 0, "message": {"role": "assistant", "content": "hi!"},
                                  "finish_reason": "length"}
    assert resp["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert resp["id"] == "c" and resp["model"] == "m"


def test_assemble_chunks_without_finish_reason_is_a_transient_error():
    chunks = [{"id": "c", "choices": [{"index": 0, "delta": {"content": "partial"}}]}]
    with pytest.raises(ProviderError) as info:
        assemble_chunks(chunks)
    assert info.value.transient is True
    with pytest.raises(ProviderError) as info:
        assemble_chunks([])
    assert info.value.transient is True


def test_assemble_chunks_tolerates_fragments_without_index_and_repeated_names():
    chunks = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"function": {"name": "bash", "arguments": "{\"a\":"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"function": {"arguments": "1}"}}]}, "finish_reason": "tool_calls"}]},
    ]
    resp = assemble_chunks(chunks)
    assert resp["choices"][0]["message"]["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"a":1}'}}]
    assert resp["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_assemble_chunks_error_chunk_is_a_non_transient_error():
    chunks = [{"choices": [{"index": 0, "delta": {"content": "a"}}]},
              {"error": {"message": "maximum context length exceeded", "type": "BadRequestError"}}]
    with pytest.raises(ProviderError) as info:
        assemble_chunks(chunks)
    assert info.value.transient is False and info.value.overflow is True


def test_response_chunks_round_trip_through_assemble(tmp_path):
    full = response(content="a b c", tool_calls=[("c1", "bash", {"command": "x"})], usage={
        "prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11})
    deltas = []
    assembled = assemble_chunks(response_chunks(full), deltas.append)
    assert assembled["choices"] == full["choices"] and assembled["usage"] == full["usage"]
    assert [d["kind"] for d in deltas] == ["content"] * 3 + ["tool_call"] * 3


def test_provider_error_flags_default_false():
    err = ProviderError(True, 503, "x")
    assert err.overflow is False and err.budget is False
    assert "overflow=False" in repr(err) and "budget=False" in repr(err)


# ---------------------------------------------------------------- DeltaRelay
def relay_run(attempts):
    """Feed attempts (lists of (kind, index, text)) through one relay with a
    restart before each, the way chat_completion does; returns what was sent."""
    out = []
    relay = DeltaRelay(out.append)
    for attempt in attempts:
        relay.restart()
        for kind, index, t in attempt:
            relay.on_delta({"kind": kind, "index": index, "text": t})
    return [(d["kind"], d["index"], d["text"]) for d in out]


def C(*texts):
    return [("content", 0, t) for t in texts]


def test_delta_relay_forwards_a_single_attempt_verbatim():
    # "I think I" repeats its own opening: within one attempt every fragment
    # is a continuation, never mistaken for a restart
    assert relay_run([C("I", " think ", "I", " can.")]) == C("I", " think ", "I", " can.")


def test_delta_relay_drops_the_re_delivered_prefix():
    assert relay_run([C("Hello ", "wor"), C("Hel", "lo world", "!")]) == C("Hello ", "wor", "ld", "!")


def test_delta_relay_marks_a_divergence_once():
    sent = relay_run([C("The answer is ", "5"), C("The answer is ", "4", " exactly")])
    assert sent == C("The answer is ", "5", DIVERGENCE_MARK + "4", " exactly")
    # a third attempt that agrees with the second adds nothing new
    assert relay_run([C("ab", "X"), C("ab", "Y"), C("abY")]) == C("ab", "X", DIVERGENCE_MARK + "Y")


def test_delta_relay_tracks_each_kind_and_index_separately():
    first = [("reasoning", 0, "hm "), ("tool_call", 0, '{"a"'), ("tool_call", 1, '{"b"')]
    second = [("reasoning", 0, "hm "), ("tool_call", 0, '{"a":1}'), ("tool_call", 1, '{"b"'),
              ("tool_call", 1, ":2}"), ("content", 0, "ok")]
    assert relay_run([first, second]) == first + [("tool_call", 0, ":1}"), ("tool_call", 1, ":2}"),
                                                   ("content", 0, "ok")]


def test_delta_relay_ignores_empty_fragments_and_is_callable():
    out = []
    relay = DeltaRelay(out.append)
    relay({"kind": "content", "index": 0, "text": ""})
    relay({"kind": "content", "index": 0, "text": "x"})
    assert out == [{"kind": "content", "index": 0, "text": "x"}]


def test_delta_relay_matches_the_reference_on_many_fragments():
    """The offset-tracking relay against the plain string rebuild it replaced,
    over many small fragments and retries that re-deliver, extend and diverge."""
    import random

    def reference(attempts):
        sent, out = {}, []
        for attempt in attempts:
            cur = {}
            for kind, index, t in attempt:
                key = (kind, index)
                s, c = sent.get(key, ""), cur.get(key, "") + t
                cur[key] = c
                if len(c) <= len(s) and s.startswith(c):
                    continue
                if c.startswith(s):
                    o = c[len(s):]
                else:
                    p = 0
                    for a, b in zip(c, s):
                        if a != b:
                            break
                        p += 1
                    o = DIVERGENCE_MARK + c[p:]
                sent[key] = c
                out.append((kind, index, o))
        return out

    rng = random.Random(7)
    for _ in range(200):
        base = "".join(rng.choice("ab ") for _ in range(rng.randint(0, 60)))
        attempts = []
        for _ in range(rng.randint(1, 4)):
            text = base[:rng.randint(0, len(base))] + "".join(rng.choice("abc") for _ in range(rng.randint(0, 20)))
            frags, i = [], 0
            while i < len(text):
                n = rng.randint(1, 5)
                frags.append((rng.choice(["content", "reasoning"]), 0, text[i:i + n]))
                i += n
            attempts.append(frags)
        assert relay_run(attempts) == reference(attempts)
    many = [C(*["x"] * 20_000)]
    assert len(relay_run(many)) == 20_000
