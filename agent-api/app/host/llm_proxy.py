"""llm_proxy — :5010 → vLLM (PLAN.md 4.1, D7, D17).

A threaded HTTP/1.1 server on the session's :5010 Unix socket. It forwards
`POST /v1/chat/completions` to LLM_URL with httpx, passes the upstream status
and body back verbatim, and records one `usage` host event per call (prompt
and completion tokens). No key injection (OpenShell's job).

Streaming: when the request body has `stream: true` the upstream call is
`client.stream(...)` and the SSE bytes are relayed as they arrive, as an
HTTP/1.1 response without Content-Length and with `Connection: close` (the
guest reads to EOF; the proxy closes that connection afterwards). The relayed
events (protocol.sse grammar) are scanned for the final usage-only chunk
(`stream_options: {include_usage: true}`): only the first event (id, model)
and events carrying a `"usage": {...}` object are parsed. The `usage` event
carries those numbers, or zeros if no usage chunk came. An upstream that
answers a stream request with a status other than 200, or with a non-SSE
body, is passed through buffered, verbatim. An upstream cut mid-stream closes
the guest's connection too (the guest's provider treats that as transient and
retries), and a guest that leaves mid-stream ends the relay; either way the
usage event records `error: "stream ended: ..."`.

Budget (GUEST.md M2): `token_budget` (HARNESS_TOKEN_BUDGET, 0 = unlimited) is
the session's allowance of `total_tokens` summed over its calls. Once the
running total reaches it, every further call is answered
`402 {"error": "budget_exhausted"}` before forwarding (also recorded as a
`usage` event with status 402). One LLMProxy serves one session, so the total
is per session.

  LLMProxy(listener, upstream_url, host_event=None, timeout_s=LLM_TIMEOUT,
           token_budget=HARNESS_TOKEN_BUDGET)
    .start()   serve in a daemon thread
    .stop()    shut down
    .tokens_used, .token_budget, .budget_exhausted()
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from typing import Callable

import httpx
from protocol.canonical import is_int
from protocol.sse import iter_sse_data

from app.config import HARNESS_TOKEN_BUDGET, LLM_TIMEOUT, LLM_URL
from app.host.uds_http import UDSHandler, UDSService

logger = logging.getLogger("host.llm_proxy")

ALLOWED_PATHS = {"/v1/chat/completions"}
BUDGET_BODY = b'{"error": "budget_exhausted"}'


class _Handler(UDSHandler):
    server_version = "sb-llm-proxy/0.2"
    log = logger

    def do_GET(self) -> None:
        self._error(404, f"no route for GET {self.path}", "not_found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body()   # always drain (keep-alive)
        if path not in ALLOWED_PATHS:
            self._error(404, f"no route for POST {path}", "not_found")
            return
        proxy: LLMProxy = self.owner
        started = time.monotonic()
        if proxy.budget_exhausted():
            logger.info("budget exhausted: %s/%s tokens", proxy.tokens_used, proxy.token_budget)
            proxy.record({"status": 402, "error": "budget_exhausted", "duration_ms": 0})
            self._reply(402, BUDGET_BODY, "application/json")
            return
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json"),
                   "Accept": self.headers.get("Accept", "application/json")}
        if _wants_stream(body):
            self._forward_stream(proxy, path, body, headers, started)
        else:
            self._forward(proxy, path, body, headers, started)

    def _reply_buffered(self, proxy: "LLMProxy", resp: httpx.Response, raw: bytes,
                        started: float) -> None:
        """An upstream answer passed back verbatim, with its usage recorded.
        Usage is recorded before the reply goes out: the guest's next call may
        arrive as soon as it has the answer, and the budget check must already
        count this one."""
        proxy.record(_usage_body(resp.status_code, raw, started))
        self._reply(resp.status_code, raw, resp.headers.get("Content-Type", "application/json"))

    def _forward(self, proxy: "LLMProxy", path: str, body: bytes, headers: dict, started: float) -> None:
        try:
            resp = proxy.client.post(path, content=body, headers=headers)
        except httpx.HTTPError as e:
            self._unreachable(proxy, e, started)
            return
        self._reply_buffered(proxy, resp, resp.content, started)

    def _forward_stream(self, proxy: "LLMProxy", path: str, body: bytes, headers: dict,
                        started: float) -> None:
        try:
            with proxy.client.stream("POST", path, content=body, headers=headers) as resp:
                content_type = resp.headers.get("Content-Type", "application/json")
                if resp.status_code != 200 or "text/event-stream" not in content_type.lower():
                    # errors, or a non-streamed answer: verbatim
                    self._reply_buffered(proxy, resp, resp.read(), started)
                    return
                scanner = _SSEUsageScanner()
                ended = self._relay_sse(resp, content_type, on_data=scanner.feed)
                scanner.finish()
                proxy.record(scanner.usage_body(
                    started, error=f"stream ended: {ended}"[:300] if ended else None))
        except httpx.HTTPError as e:
            self._unreachable(proxy, e, started)

    def _unreachable(self, proxy: "LLMProxy", e: Exception, started: float) -> None:
        logger.warning("upstream %s unreachable: %s", proxy.upstream_url, e)
        proxy.record({"status": 502, "error": str(e)[:300],
                      "duration_ms": int((time.monotonic() - started) * 1000)})
        self._error(502, f"upstream unreachable: {e}", "upstream_unreachable")


_STREAM_TRUE = (b'"stream": true', b'"stream":true')
_STREAM_FALSE = (b'"stream": false', b'"stream":false')


def _wants_stream(body: bytes) -> bool:
    """`stream: true` in the request, by byte search; the body is parsed
    only when that is ambiguous ("stream" more than once, or spaced oddly)."""
    n = body.count(b'"stream"')
    if n == 0:
        return False
    if n == 1:
        if any(p in body for p in _STREAM_TRUE):
            return True
        if any(p in body for p in _STREAM_FALSE):
            return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("stream") is True


def _usage_body(status: int, raw: bytes, started: float) -> dict:
    body: dict = {"status": status, "duration_ms": int((time.monotonic() - started) * 1000)}
    if status == 200:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        _put_usage(body, data.get("usage"))
        body["model"] = data.get("model")
        body["response_id"] = data.get("id")
    return body


def _put_usage(body: dict, usage) -> None:
    usage = usage if isinstance(usage, dict) else {}
    body["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    body["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    body["total_tokens"] = int(usage.get("total_tokens")
                               or body["prompt_tokens"] + body["completion_tokens"])


_EVENT_END = re.compile(rb"\r?\n\r?\n")
_USAGE_OBJECT = re.compile(r'"usage"\s*:\s*\{')


class _SSEUsageScanner:
    """Follows the relayed SSE bytes (protocol.sse.iter_sse_data over the
    complete events seen so far) and keeps the last `usage` dict, the response
    id and model. Only the first event and the events with a `"usage": {`
    object are parsed (vLLM sends `"usage": null` on every content chunk)."""

    def __init__(self) -> None:
        self._buf = b""
        self.usage: dict | None = None
        self.response_id = None
        self.model = None
        self.chunks = 0

    def feed(self, data: bytes) -> None:
        self._buf += data
        end = None
        for end in _EVENT_END.finditer(self._buf):
            pass
        if end is not None:
            done, self._buf = self._buf[:end.end()], self._buf[end.end():]
            self._events(done)

    def finish(self) -> None:
        if self._buf:
            self._events(self._buf)
            self._buf = b""

    def _events(self, raw: bytes) -> None:
        for payload in iter_sse_data(raw.decode("utf-8", "replace").splitlines(keepends=True)):
            self.chunks += 1
            if self.chunks > 1 and not _USAGE_OBJECT.search(payload):
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            self.response_id = self.response_id or chunk.get("id")
            self.model = self.model or chunk.get("model")
            if isinstance(chunk.get("usage"), dict):
                self.usage = chunk["usage"]

    def usage_body(self, started: float, error: str | None = None) -> dict:
        body: dict = {"status": 200, "duration_ms": int((time.monotonic() - started) * 1000),
                      "stream": True, "chunks": self.chunks}
        _put_usage(body, self.usage)
        body["model"] = self.model
        body["response_id"] = self.response_id
        if error:
            body["error"] = error
        return body


class LLMProxy(UDSService):
    handler = _Handler
    name = "llm-proxy"

    def __init__(self, listener: socket.socket, upstream_url: str = LLM_URL,
                 host_event: Callable[[str, dict], object] | None = None, *,
                 timeout_s: float = LLM_TIMEOUT, token_budget: int = HARNESS_TOKEN_BUDGET) -> None:
        super().__init__(listener)
        self.upstream_url = upstream_url.rstrip("/")
        self.host_event = host_event
        self.client = httpx.Client(base_url=self.upstream_url, timeout=httpx.Timeout(timeout_s))
        self.token_budget = max(0, int(token_budget or 0))
        self.tokens_used = 0
        self._lock = threading.Lock()
        self.calls = 0

    def budget_exhausted(self) -> bool:
        with self._lock:
            return self.token_budget > 0 and self.tokens_used >= self.token_budget

    def record(self, body: dict) -> None:
        with self._lock:
            self.calls += 1
            total = body.get("total_tokens")
            if is_int(total, 1):
                self.tokens_used += total
            body = dict(body, tokens_used=self.tokens_used)
        if self.host_event is not None:
            try:
                self.host_event("usage", body)
            except Exception as e:  # the log never breaks the proxy
                logger.warning("usage event failed: %s", e)

    def closed(self) -> None:
        self.client.close()
