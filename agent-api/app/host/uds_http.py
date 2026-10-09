"""uds_http — the threaded HTTP/1.1 server both guest-facing relays share
(llm_proxy.py on :5010, gateway_forwarder.py on :5011).

  UDSHandler       BaseHTTPRequestHandler with the shared bits: keep-alive
                   HTTP/1.1, `_reply`, `_error`, `_read_body` (Content-Length
                   or chunked; always drained so keep-alive works) and
                   `_relay_sse` (an upstream text/event-stream relayed as it
                   arrives, no Content-Length, `Connection: close`: the guest
                   reads to EOF). `self.owner` is the service.
  UDSService       one server on an already-listening Unix socket (from
                   Endpoints.listener) in a daemon thread:
                   .start(), .stop(join=True)
"""
from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

logger = logging.getLogger("host.uds_http")


class UDSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log = logger

    @property
    def owner(self):
        return self.server.owner  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        self.log.debug("%s: " + format, self.server.server_name, *args)

    def address_string(self) -> str:
        return "guest"

    def _reply(self, status: int, body: bytes, content_type: str,
               headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str) -> None:
        body = json.dumps({"error": {"message": message, "type": kind}}).encode()
        self._reply(status, body, "application/json")

    def _read_body(self) -> bytes:
        """The request body (Content-Length or chunked transfer coding)."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass                                   # trailers
                    return b"".join(parts)
                parts.append(self.rfile.read(size))
                self.rfile.readline()                          # CRLF after the chunk
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _relay_sse(self, resp, content_type: str, headers: dict | None = None,
                   on_data: Callable[[bytes], None] | None = None) -> None:
        """Relay an httpx streaming response's raw bytes as they arrive.
        An upstream cut (httpx.HTTPError) or a guest that left (OSError)
        propagates to the caller after the headers went out."""
        self.send_response(resp.status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")     # no Content-Length: EOF ends it
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        for data in resp.iter_raw():
            self.wfile.write(data)
            self.wfile.flush()
            if on_data is not None:
                on_data(data)


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, listener: socket.socket, handler: type[UDSHandler], owner, name: str) -> None:
        HTTPServer.__init__(self, listener.getsockname(), handler, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.server_name = name
        self.server_port = 0
        self.owner = owner


class UDSService:
    """Serves `handler` on `listener` in a daemon thread named `name`."""

    handler: type[UDSHandler] = UDSHandler
    name = "uds-http"

    def __init__(self, listener: socket.socket) -> None:
        self.listener = listener
        self._server = _Server(listener, self.handler, self, self.name)
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={"poll_interval": 0.2},
                                            daemon=True, name=self.name)
            self._thread.start()
        return self

    def stop(self, join: bool = True) -> None:
        """Stop accepting. join=False (from the asyncio loop: the port is
        closing around a park) does not wait for the serve loop, and leaves
        connections in flight to finish on their own threads."""
        if self._thread is None:
            return
        thread, self._thread = self._thread, None
        if not join:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
            return
        self._server.shutdown()
        thread.join(timeout=5)
        self.closed()

    def closed(self) -> None:
        """Hook: release what the service holds after a joined stop."""
