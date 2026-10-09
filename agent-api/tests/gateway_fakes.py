"""A fake mcp-gateway on 127.0.0.1 (TCP) for the M4 host tests.

Records every request as {method, path, headers (lower-cased), body};
`answered` counts the POST /mcp answers fully written.
  POST /mcp                         JSON-RPC echo `{"jsonrpc", "id", "result": {"seen": body}}`;
                                    `sse=True` answers text/event-stream in chunks;
                                    `hold` (threading.Event) delays the answer until set
  GET  /internal/tools              {"tools": tools}
  GET  /internal/upstreams/{n}/tools {"tools": upstream_tools[n]}
  POST /internal/call               call_result (default {"content": "called", "is_error": false})
`status` overrides every status.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.tools: list[dict] = []
        self.upstream_tools: dict[str, list[dict]] = {}
        self.call_result: dict = {"content": "called", "is_error": False}
        self.status = 200
        self.sse = False
        self.hold: threading.Event | None = None
        self.answered = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _record(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": body})
                return body

            def _json(self, obj, status=None) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(status or outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "none")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._record()
                if self.path == "/internal/tools":
                    self._json({"tools": outer.tools})
                elif self.path.startswith("/internal/upstreams/"):
                    name = self.path.split("/")[3]
                    self._json({"tools": outer.upstream_tools.get(name, [])})
                else:
                    self._json({"error": "nope"}, 404)

            def do_POST(self):
                body = self._record()
                if self.path == "/internal/call":
                    self._json(outer.call_result)
                    return
                if outer.hold is not None:
                    outer.hold.wait(10)
                req = json.loads(body or b"{}")
                answer = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"seen": req}}
                if not outer.sse:
                    self._json(answer)
                    outer.answered += 1
                    return
                self.send_response(outer.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for part in (b"event: message\n", b"data: " + json.dumps(answer).encode() + b"\n\n"):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                outer.answered += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def mcp_requests(self) -> list[dict]:
        return [r for r in self.requests if r["path"] == "/mcp"]

    def start(self) -> "FakeGateway":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
