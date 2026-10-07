"""LLMProxy on the :5010 Unix socket forwards to a fake upstream over TCP."""
import json

import httpx
import pytest

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
