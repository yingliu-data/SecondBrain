"""fake_llm — a scripted OpenAI-compatible chat-completions server on the
Unix socket `{dir}/v.sock_5010` (the test double for vsock port 5010).

    FakeLLM(dir, script)      script: list of response dicts | callable(request) -> response
    .start() / .stop()
    .requests                 the verbatim request bodies, in order
    .path                     the socket path

A script entry is served to the next POST /v1/chat/completions:
  - a full chat-completions response dict (has "choices") is sent as is;
  - a shorthand {"content": str} or {"tool_calls": [(id, name, args), ...]}
    or {"content", "tool_calls", "finish_reason", "usage"} is expanded by
    `response()`;
  - {"status": 502} (any int status) answers that HTTP status with a small
    JSON error body, without consuming a model response;
  - a callable gets the parsed request body and returns any of the above.
When the script is exhausted the server answers 500 with "script exhausted".

Helpers: `text(content)`, `tool_calls([...])`, `response(...)` build entries.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import threading
import time

PORT = 5010


def socket_path(directory: str, port: int = PORT) -> str:
    return os.path.join(directory, f"v.sock_{port}")


# ---------------------------------------------------------------- builders
_counter = [0]


def response(content=None, tool_calls=None, finish_reason=None, usage=None, *,
             response_id=None, model="fake-model") -> dict:
    """A full chat-completions response. tool_calls: list of (id, name, args)
    tuples or ready tool_call dicts; args may be a dict (serialized) or a raw
    string (sent verbatim, e.g. malformed JSON)."""
    calls = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            calls.append(tc)
            continue
        cid, name, args = tc
        arguments = args if isinstance(args, str) else json.dumps(args, separators=(",", ":"))
        calls.append({"id": cid, "type": "function",
                      "function": {"name": name, "arguments": arguments}})
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    if finish_reason is None:
        finish_reason = "tool_calls" if calls else "stop"
    if usage is None:
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    _counter[0] += 1
    return {
        "id": response_id or f"chatcmpl-fake-{_counter[0]}",
        "object": "chat.completion",
        "created": 1_790_000_000,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def text(content: str, **kw) -> dict:
    return response(content=content, **kw)


def tool_calls(calls, content=None, **kw) -> dict:
    return response(content=content, tool_calls=calls, **kw)


def expand(entry) -> dict:
    """A full response from a shorthand entry (or the entry itself)."""
    if "choices" in entry:
        return entry
    return response(content=entry.get("content"), tool_calls=entry.get("tool_calls"),
                    finish_reason=entry.get("finish_reason"), usage=entry.get("usage"),
                    response_id=entry.get("id"))


# ---------------------------------------------------------------- server
class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "fake-llm"
        self.server_port = PORT


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: "FakeLLM"

    def log_message(self, *args):   # quiet
        pass

    def address_string(self):
        return "unix"

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, {"error": "bad json"})
            return
        fake = self.fake
        with fake._lock:
            fake.requests.append(body)
            entry = fake._script.pop(0) if fake._script else None
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": f"no route {self.path}"})
            return
        if entry is None:
            self._send(500, {"error": "script exhausted"})
            return
        if callable(entry):
            entry = entry(body)
        if isinstance(entry, dict) and "status" in entry and "choices" not in entry:
            self._send(int(entry["status"]), {"error": entry.get("error", f"fake status {entry['status']}")})
            return
        delay = entry.pop("delay_s", 0) if isinstance(entry, dict) else 0
        if delay:
            time.sleep(delay)
        self._send(200, expand(entry))


class FakeLLM:
    def __init__(self, directory: str, script=None, port: int = PORT):
        self.dir = directory
        self.port = port
        self.path = socket_path(directory, port)
        self._script = list(script or [])
        self.requests: list = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    def push(self, *entries) -> None:
        """Append script entries after start."""
        with self._lock:
            self._script.extend(entries)

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._script)

    def start(self) -> "FakeLLM":
        if os.path.exists(self.path):
            os.unlink(self.path)
        handler = type("Handler", (_Handler,), {"fake": self})
        self._server = _UnixHTTPServer(self.path, handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-llm", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class UnixConnector:
    """connect(port) -> socket on `{dir}/v.sock_{port}` (the shape of
    guest.transport.UnixConnector, for the provider tests)."""

    def __init__(self, directory: str):
        self.dir = directory

    def connect(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path(self.dir, port))
        return sock
