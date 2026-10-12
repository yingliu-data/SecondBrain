"""guest.mcp — the MCP client over :5011 (docs/GUEST.md).

    McpClient(connector, port=protocol.ports.GATEWAY, *, path="/mcp", host="gateway.local")
        .start(call_id, name, arguments, *, attempt=0, input_response=None,
               timeout_s=None, reattach=False) -> McpCall
        .call_tool(...same...) -> McpResult           start(...).run()
    McpCall
        .run() -> McpResult        one POST; raises McpError
        .close()                   from any thread: aborts the POST (the
                                   socket is shut down, run() raises McpAborted)
    McpResult(text, is_error, input_required=None)
    McpError(message, *, status=None, timed_out=False), McpAborted(McpError)
    rpc_id(call_id, attempt) -> str
    tool_call_request(call_id, name, arguments, attempt=0, input_response=None) -> dict
    parse_call_result(result: dict) -> McpResult
    question_options(schema) -> list[str]
    answer_content(schema, answer) -> dict
    input_response(request_state, answer, schema) -> dict

Wire: HTTP/1.1 `POST /mcp` over the connector's socket
(the vsock to the host forwarder; the Unix-socket double in tests) — the same
ConnectorConnection guest.provider uses for :5010. The body is one JSON-RPC
request `{"jsonrpc": "2.0", "id", "method": "tools/call", "params": {name,
arguments[, _meta]}}` with `Content-Type: application/json` and `Accept:
application/json, text/event-stream`. The gateway is stateless: no
`initialize`, no session header. Every POST carries `X-SB-Call: <call_id>`
(the host forwarder holds the call across a park, keyed by
it); a re-attach after a park re-sends the same request with
`X-SB-Reattach: 1` and a 410 answer means the forwarder lost the call
(McpError.status 410: the caller records the op lost). A JSON body is the JSON-RPC response; an SSE
body (a gateway that streams) is read until the event that carries the
response with our id (notifications before it are skipped).

Ids are deterministic: `rpc_id(call_id, attempt)` = "<call_id>#<attempt>",
attempt 0 for the first POST of a call and +1 for every input-required
round trip (no randomness: replay and logs line up with the event log).

Results (MCP CallToolResult): the text of the `content` items joined with
"\\n" (a `resource` item contributes its `text`; other kinds a
`[<type> content]` line; `structuredContent` as JSON when there is no
content). `isError: true` -> McpResult.is_error. `_meta["sb/input_required"]`
= {request_state, message, schema, expires_in_s} -> McpResult.input_required
(3.2): the caller asks the user and re-sends the same tools/call with
`_meta["sb/input_response"]` = input_response(...).

Errors: a connection failure, a non-200 status, a body that is not a JSON-RPC
response with our id, or a JSON-RPC `error` are McpError; a socket timeout
sets `timed_out`; close() makes run() raise McpAborted. A tools/call is not
idempotent: nothing here retries.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
from dataclasses import dataclass

from guest.provider import ConnectorConnection, close_quietly, read_lines
from protocol.mcp import ACTIONS, CALL_HEADER, INPUT_REQUIRED, INPUT_RESPONSE, REATTACH_HEADER
from protocol.ports import GATEWAY
from protocol.sse import iter_sse_data

DEFAULT_PORT = GATEWAY
PATH = "/mcp"
ACCEPT = "application/json, text/event-stream"
GONE = 410                           # a re-attach the forwarder does not know
_TRUE = frozenset({"true", "yes", "y", "1", "ok", "accept", "approve", "approved"})
_FALSE = frozenset({"false", "no", "n", "0", "reject", "rejected", "deny"})


class McpError(Exception):
    def __init__(self, message: str, *, status=None, timed_out: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.timed_out = timed_out


class McpAborted(McpError):
    """close() was called while the POST ran."""


@dataclass
class McpResult:
    text: str
    is_error: bool
    input_required: dict | None = None


# ---------------------------------------------------------------- wire shapes
def rpc_id(call_id: str, attempt: int = 0) -> str:
    return f"{call_id}#{int(attempt)}"


def tool_call_request(call_id: str, name: str, arguments: dict, attempt: int = 0,
                      input_response: dict | None = None) -> dict:
    params: dict = {"name": name, "arguments": arguments if isinstance(arguments, dict) else {}}
    if input_response is not None:
        params["_meta"] = {INPUT_RESPONSE: input_response}
    return {"jsonrpc": "2.0", "id": rpc_id(call_id, attempt), "method": "tools/call",
            "params": params}


def _item_text(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    kind = item.get("type")
    if kind == "text" and isinstance(item.get("text"), str):
        return item["text"]
    if kind == "resource" and isinstance(item.get("resource"), dict) \
            and isinstance(item["resource"].get("text"), str):
        return item["resource"]["text"]
    return f"[{kind or 'unknown'} content]"


def parse_call_result(result) -> McpResult:
    """McpResult from a CallToolResult dict (module doc)."""
    if not isinstance(result, dict):
        raise McpError(f"tools/call result is not an object: {type(result).__name__}")
    content = result.get("content")
    if isinstance(content, list) and content:
        text = "\n".join(_item_text(item) for item in content)
    elif result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False, sort_keys=True)
    else:
        text = ""
    meta = result.get("_meta")
    request = meta.get(INPUT_REQUIRED) if isinstance(meta, dict) else None
    if request is not None:
        if not isinstance(request, dict) or not isinstance(request.get("request_state"), str) \
                or not request["request_state"]:
            raise McpError("malformed sb/input_required (no request_state)")
        schema = request.get("schema")
        expires = request.get("expires_in_s")
        request = {
            "request_state": request["request_state"],
            "message": request.get("message") if isinstance(request.get("message"), str) else text,
            "schema": schema if isinstance(schema, dict) else {},
            "expires_in_s": expires if isinstance(expires, (int, float))
            and not isinstance(expires, bool) and expires > 0 else None,
        }
    return McpResult(text=text, is_error=bool(result.get("isError", False)), input_required=request)


def _rpc_response(message, want_id: str):
    """The JSON-RPC response object for `want_id` in a parsed message (an
    object or a batch list), or None."""
    candidates = message if isinstance(message, list) else [message]
    for m in candidates:
        if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
            return m
    return None


def _check_rpc(response: dict) -> dict:
    if "error" in response:
        err = response["error"]
        if isinstance(err, dict):
            raise McpError(f"gateway error {err.get('code')}: {err.get('message')}")
        raise McpError(f"gateway error: {err}")
    return response["result"]


# ---------------------------------------------------------------- elicitation helpers
def _single_property(schema) -> tuple[str, dict] | None:
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and len(props) == 1:
        (name, spec), = props.items()
        return name, spec if isinstance(spec, dict) else {}
    return None


def _enum_values(spec: dict) -> list | None:
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        return list(spec["enum"])
    one_of = spec.get("oneOf")
    if isinstance(one_of, list) and one_of and all(isinstance(o, dict) and "const" in o for o in one_of):
        return [o["const"] for o in one_of]
    return None


def question_options(schema) -> list[str]:
    """The question's options: the values of a schema with exactly one
    property that is an enum; [] (free text) otherwise."""
    single = _single_property(schema)
    if single is None:
        return []
    values = _enum_values(single[1])
    return [str(v) for v in values] if values else []


def _coerce(spec: dict, answer: str):
    """The answer as the property's type (an enum value, a bool, a number),
    or the text itself when it does not fit."""
    values = _enum_values(spec)
    if values:
        for v in values:
            if str(v) == answer or str(v).lower() == answer.strip().lower():
                return v
        return answer
    kind = spec.get("type")
    word = answer.strip().lower()
    if kind == "boolean":
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        return answer
    if kind in ("integer", "number"):
        try:
            return int(word) if kind == "integer" else float(word)
        except ValueError:
            return answer
    return answer


def answer_content(schema, answer: str) -> dict:
    """The elicitation `content` for a free-text answer: a single-property
    schema gets {prop: answer} (coerced to the property's type); otherwise an
    answer that is a JSON object is used as is, else it goes into the first
    required (or first) property; {} when the schema has no properties."""
    answer = "" if answer is None else str(answer)
    single = _single_property(schema)
    if single is not None:
        return {single[0]: _coerce(single[1], answer)}
    try:
        parsed = json.loads(answer)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return {}
    required = [r for r in (schema.get("required") or []) if r in props]
    key = required[0] if required else next(iter(props))
    spec = props[key] if isinstance(props[key], dict) else {}
    return {key: _coerce(spec, answer)}


def input_response(request_state: str, answer, schema) -> dict:
    """`_meta["sb/input_response"]` for a user's answer: action "decline" or
    "cancel" when the answer is exactly that word, otherwise "accept" with
    answer_content(schema, answer)."""
    answer = "" if answer is None else str(answer)
    word = answer.strip().lower()
    if word in ACTIONS and word != "accept":
        return {"request_state": request_state, "action": word}
    return {"request_state": request_state, "action": "accept",
            "content": answer_content(schema, answer)}


# ---------------------------------------------------------------- the client
class _CallConnection(ConnectorConnection):
    """Registers its socket with the McpCall so close() can shut it down; a
    call closed before the connect finished never sends."""

    def __init__(self, call: "McpCall", *a, **kw):
        super().__init__(*a, **kw)
        self._call = call

    def connect(self):
        super().connect()
        with self._call._lock:
            if self._call._closed:
                self.sock.close()
                raise McpAborted("aborted")
            self._call._sock = self.sock


class McpCall:
    def __init__(self, client: "McpClient", call_id: str, request: dict, timeout_s,
                 reattach: bool = False):
        self.client = client
        self.call_id = call_id
        self.request = request
        self.timeout_s = timeout_s
        self.reattach = reattach
        self._lock = threading.Lock()
        self._closed = False
        self._sock: socket.socket | None = None

    @property
    def id(self) -> str:
        return self.request["id"]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self) -> McpResult:
        conn = _CallConnection(self, self.client.connector, self.client.port, self.timeout_s,
                               host=self.client.host)
        try:
            response = self._exchange(conn)
        except McpError:
            if self._closed:
                raise McpAborted("aborted") from None
            raise
        except (OSError, http.client.HTTPException) as exc:
            if self._closed:
                raise McpAborted("aborted") from None
            if isinstance(exc, socket.timeout):
                raise McpError(f"gateway timed out after {self.timeout_s:g} s", timed_out=True) from None
            raise McpError(f"gateway unreachable: {exc.__class__.__name__}: {exc}") from None
        finally:
            close_quietly(conn)
        return parse_call_result(_check_rpc(response))

    def _exchange(self, conn) -> dict:
        """POST the request; the JSON-RPC response object for our id."""
        payload = json.dumps(self.request, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "Accept": ACCEPT,
            CALL_HEADER: self.call_id,
        }
        if self.reattach:
            headers[REATTACH_HEADER] = "1"
        conn.request("POST", self.client.path, body=payload, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        if status == 200 and "text/event-stream" in (resp.getheader("Content-Type") or "").lower():
            return self._from_sse(resp)
        raw = resp.read()
        if status != 200:
            raise McpError(f"gateway HTTP {status}: {raw.decode('utf-8', 'replace')[:300]}",
                           status=status)
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise McpError(f"gateway response is not JSON: {exc}", status=status) from None
        response = _rpc_response(message, self.id)
        if response is None:
            raise McpError(f"gateway response carries no JSON-RPC response for id {self.id!r}",
                           status=status)
        return response

    def _from_sse(self, resp) -> dict:
        for data in iter_sse_data(read_lines(resp)):
            try:
                message = json.loads(data)
            except ValueError:
                continue
            response = _rpc_response(message, self.id)
            if response is not None:
                return response
        raise McpError(f"event stream ended without a response for id {self.id!r}")


class McpClient:
    def __init__(self, connector, port: int = DEFAULT_PORT, *, path: str = PATH,
                 host: str = "gateway.local"):
        self.connector = connector
        self.port = port
        self.path = path
        self.host = host

    def start(self, call_id: str, name: str, arguments: dict, *, attempt: int = 0,
              input_response: dict | None = None, timeout_s=None,
              reattach: bool = False) -> McpCall:
        return McpCall(self, call_id,
                       tool_call_request(call_id, name, arguments, attempt, input_response),
                       timeout_s, reattach=reattach)

    def call_tool(self, call_id: str, name: str, arguments: dict, **kw) -> McpResult:
        return self.start(call_id, name, arguments, **kw).run()
