"""sse — the one server-sent-events grammar for OpenAI-style chat streams
(standard library only; pure functions, no I/O).

Parsing:
  iter_sse_data(lines: Iterable[str]) -> Iterator[str]
      the `data` payload of each event, in order. Events are separated by a
      blank line; an event's `data:` lines are joined with "\\n"; the optional
      single space after the colon is dropped; `:` comment lines and the
      `event:`/`id:`/`retry:` fields are ignored; CRLF line ends are tolerated;
      an unterminated last event is flushed at EOF; stops at `[DONE]` (not
      yielded).

Framing (what a server writes):
  sse_frame(payload: str) -> bytes       b"data: <payload>\\n\\n"
  DONE_FRAME                             b"data: [DONE]\\n\\n"

Chunking (how a non-streamed chat-completions response is served as a stream,
the way vLLM does it; the test fakes use these so every fake streams alike):
  split_words(text) -> list[str]          word fragments, whitespace attached
  split_arguments(text, parts=3) -> list[str]
                                          up to `parts` pieces of a tool call's
                                          `arguments` (fewer for short text)
  response_chunks(response) -> list[dict] chat.completion.chunk dicts: a role
                                          chunk, reasoning fragments, content
                                          fragments, per tool call a first
                                          fragment {index, id, type, function
                                          {name, arguments}} then argument
                                          fragments, a finish_reason chunk, a
                                          usage-only chunk (`choices: []`)
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

DONE_MARKER = "[DONE]"
DONE_FRAME = b"data: [DONE]\n\n"

_WORDS = re.compile(r"\S+\s*|\s+")


# ---------------------------------------------------------------- parsing
def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """The `data` payload of each server-sent event, in order (see module doc)."""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                payload = "\n".join(data)
                data = []
                if payload.strip() == DONE_MARKER:
                    return
                yield payload
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep:
            field, value = line, ""
        if field != "data":
            continue
        if value.startswith(" "):
            value = value[1:]
        data.append(value)
    if data:
        payload = "\n".join(data)
        if payload.strip() != DONE_MARKER:
            yield payload


# ---------------------------------------------------------------- framing
def sse_frame(payload: str) -> bytes:
    """One single-line `data:` event."""
    return f"data: {payload}\n\n".encode("utf-8")


# ---------------------------------------------------------------- chunking
def split_words(text: str) -> list[str]:
    """Word fragments (each with its trailing whitespace) that join back to
    `text`; [] for empty text."""
    if not text:
        return []
    parts = _WORDS.findall(text)
    assert "".join(parts) == text
    return parts


def split_arguments(text: str, parts: int = 3) -> list[str]:
    """`text` cut into `parts` contiguous pieces of near-equal length (two
    when shorter than 2 * parts chars, one when shorter than 2). The pieces
    join back to `text`."""
    n = len(text)
    parts = max(1, int(parts))
    count = 1 if n < 2 else 2 if n < 2 * parts else parts
    bounds = [n * i // count for i in range(count + 1)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:])]


def response_chunks(response: dict) -> list[dict]:
    """The chat.completion.chunk dicts a full (non-streamed) chat-completions
    response is streamed as (module doc)."""
    choice = response["choices"][0]
    message = choice["message"]
    head = {"id": response.get("id"), "object": "chat.completion.chunk",
            "created": response.get("created"), "model": response.get("model")}

    def chunk(delta: dict, finish_reason=None) -> dict:
        return {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                "usage": None}

    out = [chunk({"role": "assistant", "content": ""})]
    for piece in split_words(message.get("reasoning_content") or ""):
        out.append(chunk({"reasoning_content": piece}))
    for piece in split_words(message.get("content") or ""):
        out.append(chunk({"content": piece}))
    for index, call in enumerate(message.get("tool_calls") or []):
        frags = split_arguments(call["function"]["arguments"])
        out.append(chunk({"tool_calls": [{"index": index, "id": call["id"], "type": call.get("type", "function"),
                                          "function": {"name": call["function"]["name"],
                                                       "arguments": frags[0]}}]}))
        for frag in frags[1:]:
            out.append(chunk({"tool_calls": [{"index": index, "function": {"arguments": frag}}]}))
    out.append(chunk({}, finish_reason=choice["finish_reason"]))
    out.append({**head, "choices": [], "usage": response.get("usage")})
    return out
