"""guest.provider against the scripted FakeLLM on a Unix socket."""
from __future__ import annotations

import json

import pytest

from guest.context import Request
from guest.provider import Provider, ProviderError
from tests.fakes.fake_llm import FakeLLM, UnixConnector, text, tool_calls


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
    return Provider(UnixConnector(str(tmp_path)), port=5010, model="fake-model", timeout_s=5, **kw)


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
