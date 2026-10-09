"""guest.provider — the LLM call over :5010 (PLAN.md D7, D17; docs/GUEST.md).

    Provider(connector, port=5010, model=..., timeout_s=..., retries=3, *,
             backoff_s=(1, 2, 4), sleep=time.sleep, enable_thinking=False)
        .chat_completion(request, *, stream=False, on_delta=None) -> dict
                                               the assembled OpenAI chat-completions
                                               response, non-streamed shape either way
        .usage                                 summed prompt/completion/total tokens
        .last_usage                            the last response's usage dict
        .attempts                              total HTTP attempts (tests, logs)
    ProviderError(transient, status, message, *, overflow=False, budget=False)
    DeltaRelay(send)                           the retry de-dup behind on_delta

Wire shape: POST /v1/chat/completions, `{model, messages, tools, tool_choice:
"auto", temperature, max_tokens, chat_template_kwargs: {enable_thinking:
false}}` — the shape vLLM serves. Non-streamed: `stream: false`, one JSON body.
Streamed (`stream: true, stream_options: {include_usage: true}`): the body is
text/event-stream, parsed by `protocol.sse.iter_sse_data` and assembled
(`assemble_chunks`) into the same dict the non-streamed call returns:
`content`, `reasoning_content` and `tool_calls` by `index` (ids and names from
the first fragment, `function.arguments` fragments concatenated),
`finish_reason` from the last choice chunk that carries one, `usage` from the
final usage-only chunk. The HTTP connection is an `http.client.HTTPConnection`
whose socket comes from `connector.connect(port)` (the vsock, or the
Unix-socket test double). `request.params` must not carry `stream` (the
context builder strips it; the call's `stream` argument decides).

Deltas: `on_delta({"kind": "content"|"reasoning"|"tool_call"|"text", "index":
int, "text": str})`, always before the response is returned. Streamed: once
per new fragment (an upstream that answers a streamed request with one JSON
body delivers its content, reasoning and arguments as one fragment each).
Non-streamed: one `{"kind": "text", "index": 0, "text": content}` when the
content is non-empty. A retried attempt re-streams from the start; every call
runs its deltas through one DeltaRelay that is restarted per attempt, so the
caller sees each piece of text once (DeltaRelay doc).

Errors: 502/503/504 and connection errors are transient; a stream that ends
(EOF, [DONE] or a broken connection) before a `finish_reason` arrived is a
transient cut. Transient errors are retried with backoff 1 s, 2 s, 4 s
(`backoff_s`); after `retries` failed attempts the last error is raised. Any
other status is a non-transient ProviderError: 402 sets `budget=True` (the
proxy's token budget, GUEST.md); a 400 whose body mentions "context length",
"maximum context" or "too many tokens" sets `overflow=True`. The caller (the
coordinator) records the outcome as events; this module records nothing.
"""
from __future__ import annotations

import http.client
import json
import time
from typing import Callable, Iterable, Iterator

from protocol.canonical import is_int
from protocol.sse import iter_sse_data

DEFAULT_PORT = 5010
PATH = "/v1/chat/completions"
TRANSIENT_STATUSES = frozenset({502, 503, 504})
DEFAULT_BACKOFF_S = (1.0, 2.0, 4.0)
OVERFLOW_PHRASES = ("context length", "maximum context", "too many tokens")
DIVERGENCE_MARK = "\n…\n"
# what a broken connection or a timeout raises (socket.timeout is an OSError)
_TRANSIENT_EXC = (OSError, http.client.HTTPException)


class ProviderError(Exception):
    def __init__(self, transient: bool, status, message: str, *,
                 overflow: bool = False, budget: bool = False):
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.message = message
        self.overflow = overflow      # 400: the request does not fit the context window
        self.budget = budget          # 402: the proxy's token budget is exhausted

    def __repr__(self) -> str:
        return (f"ProviderError(transient={self.transient}, status={self.status}, "
                f"message={self.message!r}, overflow={self.overflow}, budget={self.budget})")


def _mentions_overflow(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in OVERFLOW_PHRASES)


def classify_error(status: int, body_text: str) -> ProviderError:
    """The ProviderError for a non-200 HTTP status and its body."""
    if status in TRANSIENT_STATUSES:
        return ProviderError(True, status, f"upstream {status}: {body_text[:200]}")
    return ProviderError(False, status, f"upstream {status}: {body_text[:500]}",
                         overflow=(status == 400 and _mentions_overflow(body_text)),
                         budget=(status == 402))


def _transient(exc: BaseException, what: str, status=None) -> ProviderError:
    """The transient ProviderError for a connection-level failure."""
    return ProviderError(True, status, f"{what}: {exc.__class__.__name__}: {exc}")


class ConnectorConnection(http.client.HTTPConnection):
    """An HTTPConnection whose socket is whatever the connector opens (the
    LLM proxy on :5010 here, the gateway forwarder on :5011 in guest.mcp)."""

    def __init__(self, connector, port: int, timeout, host: str = "llm.local"):
        super().__init__(host, timeout=timeout)
        self._connector = connector
        self._port = port

    def connect(self):
        sock = self._connector.connect(self._port)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        self.sock = sock


# ---------------------------------------------------------------- deltas
class DeltaRelay:
    """Forwards each piece of delta text once across provider retries.

    `send(fragment)` receives what the caller should display, a
    `{"kind", "index", "text"}` dict. Per (kind, index) stream: `sent` is the
    text already forwarded, `cur` the cumulative text of the current attempt.
    A fragment is dropped while `cur` is still a prefix of `sent`, forwarded
    as the new suffix once it extends `sent`, and, when the attempt diverges
    from `sent`, forwarded as DIVERGENCE_MARK + the text from the divergence
    point (and `sent` becomes the new attempt's text). `restart()` marks a new
    attempt; the provider calls it at the top of every attempt, so the relay
    never has to guess a restart from the text."""

    def __init__(self, send: Callable[[dict], object]):
        self._send = send
        self._sent: dict = {}
        self._cur: dict = {}

    def restart(self) -> None:
        self._cur = {}

    def on_delta(self, fragment: dict) -> None:
        kind = str(fragment.get("kind", "content"))
        index = fragment.get("index", 0)
        text = str(fragment.get("text", ""))
        if not text:
            return
        key = (kind, index)
        sent = self._sent.get(key, "")
        cur = self._cur.get(key, "") + text
        self._cur[key] = cur
        if len(cur) <= len(sent) and sent.startswith(cur):
            return                                  # re-delivered: already shown
        if cur.startswith(sent):
            out = cur[len(sent):]
        else:
            p = 0
            for a, b in zip(cur, sent):
                if a != b:
                    break
                p += 1
            out = DIVERGENCE_MARK + cur[p:]
        self._sent[key] = cur
        self._send({"kind": kind, "index": index, "text": out})

    __call__ = on_delta


def _response_fragments(response: dict) -> Iterator[tuple[str, int, str]]:
    """(kind, index, text) of a whole (non-streamed) response: the shape a
    streamed request answered with one JSON body is relayed as."""
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        return
    for kind, key in (("reasoning", "reasoning_content"), ("content", "content")):
        if isinstance(message.get(key), str):
            yield kind, 0, message[key]
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            yield "tool_call", i, fn["arguments"]


# ---------------------------------------------------------------- assembly
def assemble_chunks(chunks: Iterable[dict], on_delta: Callable[[dict], object] | None = None) -> dict:
    """Fold chat.completion.chunk dicts into the non-streamed response shape,
    calling `on_delta` per fragment. Raises ProviderError(transient) when no
    `finish_reason` arrived, and a non-transient one on an `error` chunk."""
    response_id = None
    model = None
    created = None
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    choice_index = 0
    seen_chunk = False

    def emit(kind: str, index: int, text: str) -> None:
        if on_delta is not None and text:
            on_delta({"kind": kind, "index": index, "text": text})

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        seen_chunk = True
        if "error" in chunk and not chunk.get("choices"):
            err = chunk["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise classify_error(400 if _mentions_overflow(str(message)) else 500, str(message))
        response_id = response_id or chunk.get("id")
        model = model or chunk.get("model")
        created = created or chunk.get("created")
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            idx = choice.get("index")
            if is_int(idx):
                choice_index = idx
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                content.append(text)
                emit("content", choice_index, text)
            thought = delta.get("reasoning_content")
            if thought is None:
                thought = delta.get("reasoning")
            if isinstance(thought, str) and thought:
                reasoning.append(thought)
                emit("reasoning", choice_index, thought)
            for frag in delta.get("tool_calls") or []:
                if not isinstance(frag, dict):
                    continue
                index = frag.get("index")
                if not is_int(index):
                    index = len(calls) if (frag.get("id") or not calls) else max(calls)
                call = calls.setdefault(index, {"id": None, "type": "function",
                                                "function": {"name": "", "arguments": ""}})
                if frag.get("id"):
                    call["id"] = frag["id"]
                if frag.get("type"):
                    call["type"] = frag["type"]
                fn = frag.get("function") or {}
                if fn.get("name") and not call["function"]["name"]:
                    call["function"]["name"] = fn["name"]
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    call["function"]["arguments"] += args
                    emit("tool_call", index, args)
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    if finish_reason is None:
        what = "no chunks" if not seen_chunk else "no finish_reason"
        raise ProviderError(True, 200, f"stream ended before completion ({what})")
    message: dict = {"role": "assistant", "content": "".join(content) if content else None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": choice_index, "message": message, "finish_reason": finish_reason}],
        "usage": usage if usage is not None
        else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------- the provider
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
    def body_for(self, request, stream: bool = False) -> dict:
        params = dict(getattr(request, "params", None) or {})
        body = {
            "model": self.model,
            "messages": list(request.messages),
            "stream": bool(stream),
            "temperature": params.pop("temperature", 0),
            "max_tokens": params.pop("max_tokens", 512),
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        tools = list(getattr(request, "tools", None) or [])
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(params)   # any other sampling params from hello, verbatim
        return body

    # ------------------------------------------------------------ the call
    def chat_completion(self, request, *, stream: bool = False,
                        on_delta: Callable[[dict], object] | None = None) -> dict:
        stream = bool(stream)
        payload = json.dumps(self.body_for(request, stream=stream), ensure_ascii=False).encode("utf-8")
        relay = DeltaRelay(on_delta) if on_delta is not None else None
        last_error: ProviderError | None = None
        for attempt in range(self.retries):
            if attempt:
                self._sleep(self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)])
            self.attempts += 1
            if relay is not None:
                relay.restart()
            try:
                if stream:
                    response = self._stream_once(payload, relay)
                else:
                    response = self._post_once(payload)
            except ProviderError as exc:
                if not exc.transient:
                    raise
                last_error = exc
                continue
            self._record_usage(response)
            if relay is not None and not stream:     # the one whole-text delta
                content = next((t for k, _, t in _response_fragments(response) if k == "content"), "")
                relay.on_delta({"kind": "text", "index": 0, "text": content})
            return response
        assert last_error is not None
        raise last_error

    def _open(self, payload: bytes, accept: str):
        """Send the POST; return (conn, resp). Connection errors are transient."""
        conn = ConnectorConnection(self.connector, self.port, self.timeout_s)
        try:
            conn.request("POST", PATH, body=payload, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "Accept": accept,
            })
            resp = conn.getresponse()
        except _TRANSIENT_EXC as exc:
            close_quietly(conn)
            raise _transient(exc, "connection error") from exc
        return conn, resp

    @staticmethod
    def _json_response(resp) -> dict:
        """Read a whole response body: a non-200 status is classified, a 200
        must be a JSON object with choices."""
        try:
            raw = resp.read()
        except _TRANSIENT_EXC as exc:
            raise _transient(exc, "connection error") from exc
        status = resp.status
        if status != 200:
            raise classify_error(status, raw.decode("utf-8", "replace"))
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ProviderError(False, status, f"response is not JSON: {exc}") from exc
        if not isinstance(data, dict) or not data.get("choices"):
            raise ProviderError(False, status, "response has no choices")
        return data

    def _post_once(self, payload: bytes) -> dict:
        conn, resp = self._open(payload, "application/json")
        try:
            return self._json_response(resp)
        finally:
            close_quietly(conn)

    def _stream_once(self, payload: bytes, relay: DeltaRelay | None) -> dict:
        conn, resp = self._open(payload, "text/event-stream")
        try:
            content_type = (resp.getheader("Content-Type") or "").lower()
            if resp.status != 200 or "text/event-stream" not in content_type:
                # an error, or the upstream answered with one JSON body: accept it as such
                data = self._json_response(resp)
                if relay is not None:
                    for kind, index, text in _response_fragments(data):
                        relay.on_delta({"kind": kind, "index": index, "text": text})
                return data
            return assemble_chunks(self._chunks(resp), relay)
        finally:
            close_quietly(conn)

    @staticmethod
    def _chunks(resp) -> Iterator[dict]:
        """JSON chunks from the response's SSE lines; a cut or garbled stream
        is a transient ProviderError."""
        try:
            for payload in iter_sse_data(read_lines(resp)):
                try:
                    chunk = json.loads(payload)
                except ValueError as exc:
                    raise ProviderError(True, 200, f"stream chunk is not JSON: {exc}") from exc
                yield chunk
        except _TRANSIENT_EXC as exc:
            raise _transient(exc, "stream cut", 200) from exc

    def _record_usage(self, response: dict) -> None:
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            self.last_usage = None
            return
        self.last_usage = usage
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            if is_int(v):
                self.usage[k] += v


def read_lines(resp) -> Iterator[str]:
    """Decoded lines of the body (readline never splits a UTF-8 sequence)."""
    while True:
        line = resp.readline()
        if not line:
            return
        yield line.decode("utf-8", "replace")


def close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass
