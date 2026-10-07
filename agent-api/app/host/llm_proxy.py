"""llm_proxy — :5010 → vLLM (PLAN.md 4.1, D7).

A threaded HTTP/1.1 server on the session's :5010 Unix socket. It forwards
`POST /v1/chat/completions` to LLM_URL with httpx, passes the upstream status
and body back verbatim, and records one `usage` host event per call (prompt
and completion tokens from the response). No key injection (OpenShell's job),
no streaming in M1 (M2), no budgets yet.

  LLMProxy(listener, upstream_url, host_event=None, timeout_s=LLM_TIMEOUT)
    .start()   serve in a daemon thread
    .stop()    shut down
"""
from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

import httpx

from app.config import LLM_TIMEOUT, LLM_URL

logger = logging.getLogger("host.llm_proxy")

ALLOWED_PATHS = {"/v1/chat/completions"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sb-llm-proxy/0.1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        logger.debug("proxy: " + format, *args)

    def address_string(self) -> str:
        return "guest"

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str) -> None:
        body = json.dumps({"error": {"message": message, "type": kind}}).encode()
        self._reply(status, body, "application/json")

    def do_GET(self) -> None:
        self._error(404, f"no route for GET {self.path}", "not_found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""   # always drain (keep-alive)
        if path not in ALLOWED_PATHS:
            self._error(404, f"no route for POST {path}", "not_found")
            return
        proxy: LLMProxy = self.server.proxy  # type: ignore[attr-defined]
        started = time.monotonic()
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json"),
                   "Accept": self.headers.get("Accept", "application/json")}
        try:
            resp = proxy.client.post(path, content=body, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("upstream %s unreachable: %s", proxy.upstream_url, e)
            proxy.record({"status": 502, "error": str(e)[:300],
                          "duration_ms": int((time.monotonic() - started) * 1000)})
            self._error(502, f"upstream unreachable: {e}", "upstream_unreachable")
            return
        content_type = resp.headers.get("Content-Type", "application/json")
        self._reply(resp.status_code, resp.content, content_type)
        proxy.record(_usage_body(resp, started))


def _usage_body(resp: httpx.Response, started: float) -> dict:
    body: dict = {"status": resp.status_code,
                  "duration_ms": int((time.monotonic() - started) * 1000)}
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        body["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
        body["completion_tokens"] = int(usage.get("completion_tokens") or 0)
        body["total_tokens"] = int(usage.get("total_tokens")
                                   or body["prompt_tokens"] + body["completion_tokens"])
        if isinstance(data, dict):
            body["model"] = data.get("model")
            body["response_id"] = data.get("id")
    return body


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, listener: socket.socket, proxy: "LLMProxy") -> None:
        HTTPServer.__init__(self, listener.getsockname(), _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.server_name = "llm-proxy"
        self.server_port = 0
        self.proxy = proxy


class LLMProxy:
    def __init__(self, listener: socket.socket, upstream_url: str = LLM_URL,
                 host_event: Callable[[str, dict], object] | None = None, *,
                 timeout_s: float = LLM_TIMEOUT) -> None:
        self.listener = listener
        self.upstream_url = upstream_url.rstrip("/")
        self.host_event = host_event
        self.client = httpx.Client(base_url=self.upstream_url, timeout=httpx.Timeout(timeout_s))
        self._server = _Server(listener, self)
        self._thread: threading.Thread | None = None
        self.calls = 0

    def record(self, body: dict) -> None:
        self.calls += 1
        if self.host_event is not None:
            try:
                self.host_event("usage", body)
            except Exception as e:  # the log never breaks the proxy
                logger.warning("usage event failed: %s", e)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        daemon=True, name="llm-proxy")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._thread = None
        self.client.close()
