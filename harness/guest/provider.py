"""guest.provider — the LLM call over :5010 (PLAN.md D7, D17; docs/GUEST.md).

    Provider(connector, port=5010, model=..., timeout_s=..., retries=3)
        .chat_completion(request) -> dict      the OpenAI chat-completions response
        .usage                                 summed prompt/completion/total tokens
        .last_usage                            the last response's usage dict
    ProviderError(transient, status, message)

Wire shape: POST /v1/chat/completions, `stream: false` (streaming is M2),
`{model, messages, tools, tool_choice: "auto", temperature, max_tokens,
chat_template_kwargs: {enable_thinking: false}}` — the shape vLLM serves. The
HTTP connection is an `http.client.HTTPConnection` whose socket comes from
`connector.connect(port)` (the vsock, or the Unix-socket test double).

Retries: 502/503/504 and connection errors are transient and retried with
backoff 1 s, 2 s, 4 s (`backoff_s`); after `retries` failed attempts, or on a
non-transient status, ProviderError is raised. The caller (the coordinator)
records the outcome as events; this module records nothing.
"""
from __future__ import annotations

import http.client
import json
import socket
import time

DEFAULT_PORT = 5010
PATH = "/v1/chat/completions"
TRANSIENT_STATUSES = frozenset({502, 503, 504})
DEFAULT_BACKOFF_S = (1.0, 2.0, 4.0)


class ProviderError(Exception):
    def __init__(self, transient: bool, status, message: str):
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.message = message

    def __repr__(self) -> str:
        return f"ProviderError(transient={self.transient}, status={self.status}, message={self.message!r})"


class _ConnectorConnection(http.client.HTTPConnection):
    """An HTTPConnection whose socket is whatever the connector opens."""

    def __init__(self, connector, port: int, timeout):
        super().__init__("llm.local", timeout=timeout)
        self._connector = connector
        self._port = port

    def connect(self):
        sock = self._connector.connect(self._port)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        self.sock = sock


class Provider:
    def __init__(self, connector, port: int = DEFAULT_PORT, model: str = "", timeout_s: float = 120.0,
                 retries: int = 3, *, backoff_s=DEFAULT_BACKOFF_S, sleep=time.sleep,
                 enable_thinking: bool = False):
        self.connector = connector
        self.port = port
        self.model = model
        self.timeout_s = timeout_s
        self.retries = max(1, int(retries))
        self.backoff_s = tuple(backoff_s)
        self._sleep = sleep
        self.enable_thinking = enable_thinking
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_usage: dict | None = None
        self.attempts = 0          # total HTTP attempts, for tests and the log

    # ------------------------------------------------------------ request body
    def body_for(self, request) -> dict:
        params = dict(getattr(request, "params", None) or {})
        body = {
            "model": self.model,
            "messages": list(request.messages),
            "stream": False,
            "temperature": params.pop("temperature", 0),
            "max_tokens": params.pop("max_tokens", 512),
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        tools = list(getattr(request, "tools", None) or [])
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(params)   # any other sampling params from hello, verbatim
        return body

    # ------------------------------------------------------------ the call
    def chat_completion(self, request) -> dict:
        payload = json.dumps(self.body_for(request), ensure_ascii=False).encode("utf-8")
        last_error: ProviderError | None = None
        for attempt in range(self.retries):
            if attempt:
                delay = self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)]
                self._sleep(delay)
            self.attempts += 1
            try:
                response = self._post_once(payload)
            except ProviderError as exc:
                if not exc.transient:
                    raise
                last_error = exc
                continue
            self._record_usage(response)
            return response
        assert last_error is not None
        raise last_error

    def _post_once(self, payload: bytes) -> dict:
        conn = _ConnectorConnection(self.connector, self.port, self.timeout_s)
        try:
            try:
                conn.request("POST", PATH, body=payload, headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Accept": "application/json",
                })
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException, socket.timeout) as exc:
                raise ProviderError(True, None, f"connection error: {exc.__class__.__name__}: {exc}") from exc
            status = resp.status
            if status in TRANSIENT_STATUSES:
                raise ProviderError(True, status, f"upstream {status}: {raw[:200].decode('utf-8', 'replace')}")
            if status != 200:
                raise ProviderError(False, status, f"upstream {status}: {raw[:500].decode('utf-8', 'replace')}")
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise ProviderError(False, status, f"response is not JSON: {exc}") from exc
            if not isinstance(data, dict) or not data.get("choices"):
                raise ProviderError(False, status, "response has no choices")
            return data
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _record_usage(self, response: dict) -> None:
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            self.last_usage = None
            return
        self.last_usage = usage
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                self.usage[k] += v
