"""LLMProxy on the :5010 Unix socket forwards to a fake upstream over TCP."""
import asyncio
import json

import httpx
import pytest
from protocol.sse import iter_sse_data

from app.config import VENDORS
from app.host.endpoints import Endpoints
from app.host.llm_proxy import LLMProxy
from app.skills.avatar_control.planner_llm import PlannerLLM
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


def test_gzipped_stream_is_relayed_decoded(proxy):
    p, upstream, usage, client = proxy
    upstream.gzip = True
    upstream.responses = [{"content": "one two three"}]
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m", "messages": [], "stream": True,
                             "stream_options": {"include_usage": True}}) as r:
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        received = b"".join(r.iter_raw()).decode()
    assert received == upstream.streamed[0]                  # the plain SSE text
    assert usage[0][1]["total_tokens"] == 108


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


def test_proxies_of_one_upstream_share_one_client_that_outlives_them(tmp_path):
    """One httpx.Client per upstream per process (http_clients): two
    sessions' proxies share it, and stopping one leaves it open for the other."""
    from app.host.gateway_forwarder import GatewayRelay
    upstream = FakeUpstreamLLM([{"content": "a"}, {"content": "b"}]).start()
    a, b = Endpoints(tmp_path / "a", [5010]), Endpoints(tmp_path / "b", [5010])
    pa = LLMProxy(a.listener(5010), upstream.url, timeout_s=5).start()
    pb = LLMProxy(b.listener(5010), upstream.url, timeout_s=5).start()
    try:
        assert pa.client is pb.client
        pa.stop()
        assert not pb.client.is_closed
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(b.path(5010))),
                          base_url="http://llm") as client:
            r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
            assert r.status_code == 200
        r1 = GatewayRelay("http://127.0.0.1:9", user="u", tenant="t", session="s1")
        r2 = GatewayRelay("http://127.0.0.1:9", user="u", tenant="t", session="s2")
        assert r1.client is r2.client and r1.client is not pb.client
        r1.close()
        assert not r2.client.is_closed
    finally:
        pb.stop()
        a.close()
        b.close()
        upstream.stop()


# ---------------------------------------------------------------- vendors
GUEST_BODY = {"model": "m", "messages": [], "max_tokens": 5, "temperature": 0,
              "chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("vendor", list(VENDORS))
def test_vendor_request_shape(tmp_path, monkeypatch, vendor, stream):
    """Plan feat/52 step 2 (issue #52: each vendor answers a chat turn, streamed and
    whole): the path, auth header and body that reach the upstream; the key is in
    no usage event."""
    key_env = VENDORS[vendor].key_env
    if key_env:
        monkeypatch.setenv(key_env, "sk-secret")
    upstream = FakeUpstreamLLM([{"content": "hi"}]).start()
    ep = Endpoints(tmp_path, [5010])
    usage: list = []
    p = LLMProxy(ep.listener(5010), upstream.url, lambda t, b: usage.append(b), timeout_s=5,
                 vendor=vendor, api_key_env=key_env).start()
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5010))),
                          base_url="http://llm") as client:
            r = client.post("/v1/chat/completions", json={**GUEST_BODY, "stream": stream})
    finally:
        p.stop()
        ep.close()
        upstream.stop()
    assert r.status_code == 200 and "hi" in r.text
    assert upstream.paths == [VENDORS[vendor].chat_path]
    assert upstream.headers[0].get("authorization") == ("Bearer sk-secret" if key_env else None)
    expected = {**GUEST_BODY, "stream": stream}
    if vendor != "vllm":
        del expected["chat_template_kwargs"]
    if vendor == "openai":
        expected["max_completion_tokens"] = expected.pop("max_tokens")
    if vendor == "anthropic":
        del expected["temperature"]
    assert upstream.requests == [expected]
    assert usage[0]["total_tokens"] == 108 and "sk-secret" not in json.dumps(usage)


def test_planner_uses_vendor(monkeypatch):
    """Plan feat/52 step 2: the avatar planner, the only other LLM caller, takes the
    vendor's path, key and body rules."""
    monkeypatch.setenv("GEMINI_API_KEY", "sk-secret")
    upstream = FakeUpstreamLLM([{"content": "plan"}]).start()
    try:
        planner = PlannerLLM(upstream.url, timeout_s=5, vendor="gemini", api_key_env="GEMINI_API_KEY")
        resp = asyncio.run(planner.chat_completion([], max_tokens=5,
                                                   chat_template_kwargs={"enable_thinking": False}))
    finally:
        upstream.stop()
    assert resp["choices"][0]["message"]["content"] == "plan"
    assert upstream.paths == ["/v1beta/openai/chat/completions"]
    assert upstream.headers[0]["authorization"] == "Bearer sk-secret"
    assert upstream.requests[0]["max_tokens"] == 5 and "chat_template_kwargs" not in upstream.requests[0]
