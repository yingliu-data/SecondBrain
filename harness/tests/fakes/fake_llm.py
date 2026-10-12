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
    JSON error body, without consuming a model response; {"status": 400,
    "message": "..."} sends an OpenAI-style `{"error": {"message", "type"}}`
    body with that message (e.g. a context-length overflow); {"status": 402}
    is the proxy's budget answer `{"error": "budget_exhausted"}`;
  - a callable gets the parsed request body and returns any of the above.
When the script is exhausted the server answers 500 with "script exhausted".

Streaming (the request has `stream: true`): the same entry is served as
text/event-stream in chunked HTTP/1.1, the way vLLM streams it — a role
chunk, `reasoning_content` and `content` split into word fragments, each
tool call as a first fragment with `index`, `id`, `name` and the first piece
of `arguments` followed by 2–3 `arguments` fragments, a finish_reason chunk,
a usage-only chunk (`choices: []`), then `data: [DONE]`. Entry options:
  - {"cut_after": n, ...}: close the connection after n data chunks, without
    finish_reason or [DONE] (a cut stream; content defaults to a long answer);
  - {"keepalive": True, ...}: a `: keep-alive` comment line before every chunk;
  - {"multiline": True, ...}: each chunk's JSON is sent over two `data:` lines.
`.streamed` records the SSE text sent per streamed call (for verbatim checks).

Helpers: `text(content)`, `tool_calls([...])`, `response(...)` build entries.
Chunking and framing come from protocol.sse (`response_chunks`, `split_words`,
`split_arguments`, `sse_frame`, `DONE_FRAME`, re-exported here);
`write_sse(wfile, chunks, ...)` writes a chunked-HTTP SSE body and is shared
with the agent-api fake so both servers stream byte-for-byte alike.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import threading
import time

from protocol.ports import LLM, socket_name
from protocol.uds import connect_unix
from protocol.sse import DONE_FRAME, response_chunks, split_arguments, split_words, sse_frame  # noqa: F401

PORT = LLM


def socket_path(directory: str, port: int = PORT) -> str:
    return os.path.join(directory, socket_name(port))


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


STREAM_OPTIONS = ("cut_after", "keepalive", "multiline", "delay_s")


def write_sse(wfile, chunks, *, keepalive: bool = False, cut_after=None, multiline: bool = False,
              on_text=None) -> str:
    """Write `chunks` (chat.completion.chunk dicts) to `wfile` as an SSE body
    in chunked transfer encoding, after the caller sent the 200 and its
    headers (`Content-Type: text/event-stream`, `Transfer-Encoding: chunked`).

    keepalive: a `: keep-alive` comment before every event; multiline: each
    event's JSON over two `data:` lines; cut_after=n: stop after n events with
    neither `[DONE]` nor the terminating zero-length chunk (the caller then
    sets `close_connection`: the stream is cut). `on_text(text)` gets the
    whole SSE text before the first byte is written (tests record it before
    the client can see the end). Returns that text; it ends with DONE_FRAME
    unless the stream was cut."""
    frames: list[bytes] = []
    cut = False
    for n, chunk in enumerate(chunks, start=1):
        if cut_after is not None and n > int(cut_after):
            cut = True
            break
        if keepalive:
            frames.append(b": keep-alive\n\n")
        payload = json.dumps(chunk, separators=(",", ":"))
        if multiline:
            mid = payload.index(',"choices"') + 1      # a top-level comma: still JSON once joined
            frames.append(f"data: {payload[:mid]}\ndata: {payload[mid:]}\n\n".encode("utf-8"))
        else:
            frames.append(sse_frame(payload))
    if not cut:
        frames.append(DONE_FRAME)
    text_ = b"".join(frames).decode("utf-8")
    if on_text is not None:
        on_text(text_)
    for i, data in enumerate(frames):
        out = b"%x\r\n%s\r\n" % (len(data), data)
        if not cut and i == len(frames) - 1:
            out += b"0\r\n\r\n"      # with [DONE]: a client may close as soon as it sees it
        wfile.write(out)
        wfile.flush()
    return text_


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
            status = int(entry["status"])
            if "message" in entry:
                payload = {"error": {"message": entry["message"], "type": entry.get("type", "invalid_request_error")}}
            elif status == 402:
                payload = {"error": entry.get("error", "budget_exhausted")}
            else:
                payload = {"error": entry.get("error", f"fake status {status}")}
            self._send(status, payload)
            return
        options = {k: entry.pop(k) for k in STREAM_OPTIONS if k in entry} if isinstance(entry, dict) else {}
        delay = options.get("delay_s", 0)
        if delay:
            time.sleep(delay)
        if body.get("stream") is True:
            if "cut_after" in options and not entry:
                entry = {"content": "this answer is long enough to be cut in the middle of it"}
            self._stream(expand(entry), options)
            return
        self._send(200, expand(entry))

    def _stream(self, full: dict, options: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def record(text_: str) -> None:
            with self.fake._lock:             # recorded before the client can see the end
                self.fake.streamed.append(text_)

        sent = write_sse(self.wfile, response_chunks(full), keepalive=bool(options.get("keepalive")),
                         cut_after=options.get("cut_after"), multiline=bool(options.get("multiline")),
                         on_text=record)
        if not sent.endswith(DONE_FRAME.decode()):
            self.close_connection = True      # no [DONE], no terminating chunk: the stream is cut


class FakeLLM:
    def __init__(self, directory: str, script=None, port: int = PORT):
        self.dir = directory
        self.port = port
        self.path = socket_path(directory, port)
        self._script = list(script or [])
        self.requests: list = []
        self.streamed: list = []        # the SSE text sent per streamed call
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
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="fake-llm", daemon=True)
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
        return connect_unix(socket_path(self.dir, port))
