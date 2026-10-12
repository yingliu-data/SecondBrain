"""fake_gateway — the host forwarder + MCP gateway double on
`{dir}/v.sock_5011`, for the guest tests.

    FakeGateway(dir, tools=None).start() / .stop()
    .requests          every POST /mcp: {"headers": {lower-case name: value}, "body": dict}
    .calls             call_id -> how many times a tool actually executed for it
                       (a re-attach never adds one)
    .release(name)     let a gated tool (GATED) finish
    .forget(call_id)   drop a held call (the forwarder lost its table): a
                       re-attach then answers 410
    .wait_requests(n, timeout)  until n POSTs have arrived
    .wait_done(call_id, timeout) until the held call has its response

Wire, as the guest sees it through the forwarder: `POST /mcp` with one
JSON-RPC request -> one JSON body (stateless, `json_response`), methods
`tools/list` and `tools/call`; any other path or method -> 404. Tool failures
are results with `isError: true`, malformed requests JSON-RPC errors.

Forwarder semantics (3.3): every tools/call carries `X-SB-Call`; the tool runs
on its own thread, independent of the guest's connection, and its response is
kept per call_id. `X-SB-Reattach: 1` joins the held call (the stored response
at once when it is done); an unknown call_id on a re-attach -> 410. A first
send whose call_id is already held (and that carries no
`sb/input_response`) is a re-attach too; one with an input_response replaces
the entry (a new request).

Tools (DEFAULT_TOOLS; `tools` adds or overrides `name -> fn(args, meta) ->
CallToolResult dict`):
  wcc-events__draft_event   "Drafted <title> on <date>"
  wcc-events__fail          isError: "upstream said no"
  wcc-events__slow          waits for release("wcc-events__slow") (or 30 s)
  wcc-events__approve       elicits {decision: approve|reject}; accept ->
                            "approved: <decision>", decline/cancel -> isError,
                            an unknown request_state -> isError "input request
                            expired"; with `rounds: 2` it asks twice
  search_tools / describe_tool / call_tool   the meta tools over the above
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import threading

from protocol.ports import GATEWAY, socket_name

PORT = GATEWAY
GATED = "wcc-events__slow"
APPROVAL_SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["approve", "reject"]}}, "required": ["decision"]}


def text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def input_required(request_state: str, message: str, schema: dict, expires_in_s=3600) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": False,
            "_meta": {"sb/input_required": {"request_state": request_state, "message": message,
                                            "schema": schema, "expires_in_s": expires_in_s}}}


class FakeGateway:
    def __init__(self, dir, tools=None):
        self.dir = str(dir)
        self.path = os.path.join(self.dir, socket_name(PORT))
        self.requests: list[dict] = []
        self.calls: dict[str, int] = {}
        self._held: dict[str, dict] = {}          # call_id -> {"done": Event, "response": (status, bytes)}
        self._states: dict[str, dict] = {}        # request_state -> {call_id, round}
        self._gates: dict[str, threading.Event] = {}
        self._asked = 0
        self._cv = threading.Condition()
        self._server = None
        self._thread = None
        self.tools = {
            "wcc-events__draft_event": lambda a, m: text_result(
                f"Drafted {a.get('title')} on {a.get('date')}"),
            "wcc-events__fail": lambda a, m: text_result("upstream said no", True),
            "wcc-events__slow": self._slow,
            "wcc-events__approve": self._approve,
        }
        self.tools.update(tools or {})
        self.meta = {"search_tools": self._search, "describe_tool": self._describe,
                     "call_tool": self._call_tool}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "FakeGateway":
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        gw = self

        class Handler(_Handler):
            gateway = gw

        self._server = _Server(self.path, Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-gateway",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        for gate in list(self._gates.values()):
            gate.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ test controls
    def gate(self, name: str) -> threading.Event:
        with self._cv:
            return self._gates.setdefault(name, threading.Event())

    def release(self, name: str = GATED) -> None:
        self.gate(name).set()

    def forget(self, call_id: str) -> None:
        with self._cv:
            self._held.pop(call_id, None)

    def wait_requests(self, n: int, timeout: float = 10.0) -> list:
        with self._cv:
            if not self._cv.wait_for(lambda: len(self.requests) >= n, timeout):
                raise TimeoutError(f"{len(self.requests)} of {n} gateway requests")
            return list(self.requests)

    def wait_done(self, call_id: str, timeout: float = 10.0) -> None:
        with self._cv:
            held = self._held.get(call_id)
        if held is None or not held["done"].wait(timeout):
            raise TimeoutError(f"held call {call_id} not done")

    # ------------------------------------------------------------ the forwarder
    def handle(self, headers: dict, raw: bytes) -> tuple[int, bytes]:
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return 200, _rpc_error(None, -32700, "parse error")
        with self._cv:
            self.requests.append({"headers": headers, "body": body})
            self._cv.notify_all()
        method = body.get("method") if isinstance(body, dict) else None
        if method == "tools/list":
            return 200, _rpc(body.get("id"), {"tools": self._list()})
        if method != "tools/call":
            return 200, _rpc_error(body.get("id") if isinstance(body, dict) else None, -32601,
                                   f"method {method!r} not found")
        call_id = headers.get("x-sb-call")
        if not call_id:
            return 200, self._execute(body)       # no forwarder table without X-SB-Call
        params = body.get("params") or {}
        fresh = isinstance(params.get("_meta"), dict) and "sb/input_response" in params["_meta"]
        with self._cv:
            held = self._held.get(call_id)
            if headers.get("x-sb-reattach") == "1":
                if held is None:
                    return 410, b'{"error": "unknown call"}'
            elif held is None or fresh:
                held = {"done": threading.Event(), "response": None}
                self._held[call_id] = held
                self.calls[call_id] = self.calls.get(call_id, 0) + 1
                threading.Thread(target=self._run_held, args=(held, body), daemon=True).start()
        held["done"].wait(60)
        return held["response"] or (504, b"{}")

    def _run_held(self, held: dict, body: dict) -> None:
        response = self._execute(body)
        with self._cv:
            held["response"] = (200, response)
            held["done"].set()
            self._cv.notify_all()

    def _execute(self, body: dict) -> bytes:
        params = body.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        meta = params.get("_meta") or {}
        fn = self.meta.get(name) or self.tools.get(name)
        if fn is None:
            return _rpc(body.get("id"), text_result(f"unknown tool {name!r}", True))
        try:
            result = fn(args, meta)
        except Exception as exc:   # noqa: BLE001
            result = text_result(f"tool crashed: {exc!r}", True)
        return _rpc(body.get("id"), result)

    # ------------------------------------------------------------ tools
    def _list(self) -> list:
        tools = [{"name": n, "description": f"fake {n}", "inputSchema": {"type": "object"}}
                 for n in sorted(self.tools)]
        return tools + [{"name": n, "description": f"meta {n}", "inputSchema": {"type": "object"}}
                        for n in sorted(self.meta)]

    def _search(self, args, meta):
        q = str(args.get("query", "")).lower()
        hits = [{"name": n, "description": f"fake {n}"} for n in sorted(self.tools) if q in n.lower()]
        return text_result(json.dumps(hits[: int(args.get("limit", 10))]))

    def _describe(self, args, meta):
        name = args.get("name")
        if name not in self.tools:
            return text_result(f"unknown tool {name!r}", True)
        return text_result(json.dumps({"name": name, "description": f"fake {name}",
                                       "inputSchema": {"type": "object"}, "pin": "ab" * 32}))

    def _call_tool(self, args, meta):
        fn = self.tools.get(args.get("name"))
        if fn is None:
            return text_result(f"unknown tool {args.get('name')!r}", True)
        return fn(args.get("arguments") or {}, meta)

    def _slow(self, args, meta):
        self.gate(GATED).wait(float(args.get("seconds", 30)))
        return text_result("slow done")

    def _approve(self, args, meta):
        response = meta.get("sb/input_response")
        rounds = int(args.get("rounds", 1))
        if response is None:
            return self._ask(0)
        with self._cv:
            state = self._states.pop(response.get("request_state"), None)
        if state is None:
            return text_result("input request expired", True)
        action = response.get("action")
        if action != "accept":
            return text_result(f"input request {action}ed by the user", True)
        decision = (response.get("content") or {}).get("decision")
        if state["round"] + 1 < rounds:
            return self._ask(state["round"] + 1)
        return text_result(f"approved: {decision}")

    def _ask(self, round_: int) -> dict:
        with self._cv:
            self._asked += 1
            rs = f"rs-{self._asked}"
            self._states[rs] = {"round": round_}
        return input_required(rs, f"Approve the draft? (round {round_ + 1})", APPROVAL_SCHEMA)


def _rpc(id_, result) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}).encode("utf-8")


def _rpc_error(id_, code, message) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": code, "message": message}}).encode("utf-8")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway: FakeGateway

    def address_string(self):
        return "guest"

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path != "/mcp":
            return self._send(404, b'{"error": "not found"}')
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, body = self.gateway.handle(headers, raw)
        self._send(status, body)

    def do_GET(self):
        self._send(404, b'{"error": "not found"}')

    def _send(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass
        self.close_connection = True
