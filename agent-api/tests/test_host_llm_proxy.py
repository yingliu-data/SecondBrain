"""LLMProxy on the :5010 Unix socket forwards to a fake upstream over TCP."""
import json

import httpx
import pytest
from protocol.sse import iter_sse_data

from app.host.endpoints import Endpoints
from app.host.llm_proxy import LLMProxy
from host_fakes import FakeUpstreamLLM


@pytest.fixture
def proxy(tmp_path):
    upstream = FakeUpstreamLLM([{"content": "hi"}]).start()
    ep = Endpoints(tmp_path, [5010])
    usage: list = []
    p = LLMProxy(ep.listener(5010), upstream.url, lambda t, b: usage.append((t, b)), timeout_s=5)
    p.start()
    client = httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5010))),
                          base_url="http://llm")
    yield p, upstream, usage, client
    client.close()
    p.stop()
    ep.close()
    upstream.stop()


def test_forwards_status_body_and_records_usage(proxy):
    p, upstream, usage, client = proxy
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert upstream.requests[0]["model"] == "m"
    assert usage[0][0] == "usage"
    body = usage[0][1]
    assert body["prompt_tokens"] == 101 and body["completion_tokens"] == 7
    assert body["status"] == 200 and body["response_id"] == "chatcmpl-fake-1"
    # keep-alive: a second call on the same client works
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 200 and p.calls == 2


def test_upstream_error_status_is_passed_verbatim(proxy):
    p, upstream, usage, client = proxy
    upstream.status = 503
    r = client.post("/v1/chat/completions", json={})
    assert r.status_code == 503 and r.json() == {"error": "nope"}
    assert usage[-1][1]["status"] == 503 and "prompt_tokens" not in usage[-1][1]


def test_unknown_path_and_unreachable_upstream(tmp_path):
    ep = Endpoints(tmp_path, [5010])
    p = LLMProxy(ep.listener(5010), "http://127.0.0.1:9", timeout_s=2)
    p.start()
    client = httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5010))),
                          base_url="http://llm")
    try:
        assert client.post("/v1/other", json={}).status_code == 404
        assert client.get("/health").status_code == 404
        r = client.post("/v1/chat/completions", json={})
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "upstream_unreachable"
    finally:
        client.close()
        p.stop()
        ep.close()


# ---------------------------------------------------------------- streaming
def sse_events(text: str) -> list:
    """The JSON payloads of `data:` events in SSE text (without [DONE])."""
    return [json.loads(d) for d in iter_sse_data(text.splitlines(keepends=True))]


def test_stream_is_relayed_verbatim_and_usage_recorded(proxy):
    p, upstream, usage, client = proxy
    upstream.responses = [{"content": "one two three"}]
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m", "messages": [], "stream": True,
                             "stream_options": {"include_usage": True}}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "content-length" not in r.headers
        assert r.headers.get("connection") == "close"
        received = b"".join(r.iter_raw()).decode()
    assert upstream.requests[0]["stream"] is True
    assert received == upstream.streamed[0]                  # byte-for-byte relay
    events = sse_events(received)
    assert [e["choices"][0]["delta"].get("content") for e in events[1:4]] == ["one ", "two ", "three"]
    assert events[-1]["choices"] == [] and events[-1]["usage"]["total_tokens"] == 108
    assert received.endswith("data: [DONE]\n\n")
    assert usage[0][0] == "usage"
    body = usage[0][1]
    assert body["status"] == 200 and body["stream"] is True
    assert body["prompt_tokens"] == 101 and body["completion_tokens"] == 7 and body["total_tokens"] == 108
    assert body["response_id"] == "chatcmpl-fake-1" and body["model"] == "m"
    assert body["chunks"] == len(events) and body["tokens_used"] == 108
    assert p.tokens_used == 108
    # the proxy is still serving: a second (non-stream) call on a fresh connection works
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 200 and p.calls == 2


def test_stream_with_tool_calls_and_keepalive_comments_is_relayed(proxy):
    p, upstream, usage, client = proxy
    upstream.keepalive = True
    upstream.responses = [{"content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}}]}]
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": [], "stream": True})
    assert r.status_code == 200
    text = r.text
    assert text == upstream.streamed[0] and text.startswith(": keep-alive\n\n")
    frags = [tc for e in sse_events(text) for c in e["choices"] for tc in (c["delta"].get("tool_calls") or [])]
    assert frags[0]["id"] == "call_1" and frags[0]["function"]["name"] == "bash"
    assert json.loads("".join(f["function"]["arguments"] for f in frags)) == {"command": "echo hi"}
    assert usage[0][1]["total_tokens"] == 108 and usage[0][1]["status"] == 200


def test_non_stream_request_is_unchanged_by_the_streaming_path(proxy):
    p, upstream, usage, client = proxy
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": [], "stream": False})
    assert r.status_code == 200 and r.headers.get("content-length")
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert upstream.streamed == [] and "stream" not in usage[0][1]


def test_upstream_400_on_a_stream_request_is_passed_through_verbatim(proxy):
    p, upstream, usage, client = proxy
    upstream.status = 400
    upstream.error_body = {"object": "error", "message": "This model's maximum context length is 32768 tokens",
                           "type": "BadRequestError", "code": 400}
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": [], "stream": True})
    assert r.status_code == 400
    assert r.json() == upstream.error_body
    assert r.headers["content-type"] == "application/json" and r.headers.get("content-length")
    assert usage[-1][1]["status"] == 400 and "prompt_tokens" not in usage[-1][1]
    assert p.tokens_used == 0


def test_upstream_cut_mid_stream_closes_the_guest_connection(proxy):
    p, upstream, usage, client = proxy
    upstream.cut_after = 2
    upstream.responses = [{"content": "a long answer that is cut"}]
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m", "messages": [], "stream": True}) as r:
        assert r.status_code == 200
        received = b"".join(r.iter_raw()).decode()
    assert received == upstream.streamed[0] and "[DONE]" not in received
    body = usage[0][1]
    assert body["status"] == 200 and body["chunks"] == 2 and body["total_tokens"] == 0
    assert body["error"].startswith("stream ended: ")


def test_budget_answers_402_once_the_total_is_reached(tmp_path):
    upstream = FakeUpstreamLLM([{"content": "hi"}]).start()      # 108, 109, ... total_tokens per call
    ep = Endpoints(tmp_path, [5010])
    usage: list = []
    p = LLMProxy(ep.listener(5010), upstream.url, lambda t, b: usage.append(b), timeout_s=5,
                 token_budget=250)
    p.start()
    client = httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5010))), base_url="http://llm")
    try:
        assert client.post("/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 200
        assert p.tokens_used == 108 and not p.budget_exhausted()
        r = client.post("/v1/chat/completions", json={"model": "m", "messages": [], "stream": True})
        assert r.status_code == 200
        assert p.tokens_used == 217 and not p.budget_exhausted()
        r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
        assert r.status_code == 200 and p.tokens_used == 327 and p.budget_exhausted()
        # over budget: refused before forwarding, stream or not
        for body in ({"model": "m", "messages": []}, {"model": "m", "messages": [], "stream": True}):
            r = client.post("/v1/chat/completions", json=body)
            assert r.status_code == 402 and r.json() == {"error": "budget_exhausted"}
        assert len(upstream.requests) == 3
        assert usage[-1]["status"] == 402 and usage[-1]["error"] == "budget_exhausted"
        assert usage[-1]["tokens_used"] == 327 and "token_budget" not in usage[-1]
        assert p.calls == 5
    finally:
        client.close()
        p.stop()
        ep.close()
        upstream.stop()


def test_zero_budget_is_unlimited(proxy):
    p, upstream, usage, client = proxy
    assert p.token_budget == 0
    for _ in range(3):
        assert client.post("/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 200
    assert p.tokens_used > 0 and not p.budget_exhausted()
