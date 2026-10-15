"""stream — guest events → the SSE strings the clients already consume.

The strings are byte-identical to the retired legacy loop's: `token` payloads are
`json.dumps({'text': word + ' '})` word by word (the iPhone's TTS expects
the word-wise emission), `tool_call` is `{id, name, arguments}`,
`avatar_command` is `{name, result}`, `done` is `{}`. Added for the harness:
`seq{input_id, seq}` on every input, `question{...}` and `error{...}`.

Answer text. The guest sends live `delta{session_id, turn_id, kind, index,
text}` messages before the turn's `llm_response`: `kind: "content"` is one
streamed fragment (relayed verbatim as one token), `kind: "text"` is the
whole message of a non-streamed turn (relayed word-wise, the legacy shape);
reasoning and tool_call fragments are never shown, nor is a compaction turn's
summary. A turn that sent text adds
nothing at its `llm_response`; one that did not (a guest without deltas) goes
out word-wise there; a task that sent no text at all gets the settle answer
word-wise. The settle adds no `done`: an input's stream ends only at the
worker's finish_input (fatal_items on failure). When a turn's text follows an earlier turn of the task whose text
did not end in whitespace ("Let me check." then a tool call, or "Done." then
a follow-up), one `token "\\n"` separates them.

  Projector()                 per session; remembers call_id → tool name and
                              the text sent per turn of the running task
    .begin_task()             a task is about to start: nothing sent yet
    .project(event) -> [sse]  one stored event
    .live(msg) -> [sse]       one live message (delta → token)
    .note_host_result(call_id, content)
                              the verbatim host-skill result (the guest may
                              receive a compacted copy, e.g. plan_movement)
  project(event) -> [sse]     stateless convenience (a fresh Projector)
  sse(event, data) -> str     the one formatter
  tokens(text) -> [sse]       word-wise token events
  fatal_items(message, **extra) -> [sse]
                              a fatal `error{class, message, **extra}` + `done`
  KEEPALIVE                   ": keepalive\\n\\n", every 15 s while a task runs
  SSE_HEADERS                 the response headers of every SSE route
"""
from __future__ import annotations

import json

from app.skills.avatar_control import AVATAR_TOOLS

KEEPALIVE = ": keepalive\n\n"

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-store, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def tokens(text: str) -> list[str]:
    """Word-wise token events, exactly as the legacy loop streams a reply."""
    return [sse("token", {"text": word + " "}) for word in text.split(" ")]


DONE = "event: done\ndata: {}\n\n"


def fatal_items(message: str, **extra) -> list[str]:
    """The end of a failed input's stream (before its end-of-stream)."""
    return [sse("error", {"class": "fatal", "message": message, **extra}), DONE]

NEWLINE = sse("token", {"text": "\n"})


class Projector:
    def __init__(self) -> None:
        self._names: dict[str, str] = {}        # call_id -> tool name
        self._host_results: dict[str, str] = {}
        # turn_id -> the last character of the text sent for it ("" for none;
        # this task, in order): all the separator needs
        self._live: dict[str, str] = {}
        self._compaction: set[str] = set()      # compaction turn_ids: their summary is never shown

    def begin_task(self) -> None:
        """The worker is about to send a task: nothing has been sent for it
        yet (called before the control message, so before any delta)."""
        self._live.clear()

    def note_host_result(self, call_id: str, content: str) -> None:
        self._host_results[call_id] = content

    def tool_name(self, call_id: str) -> str | None:
        return self._names.get(call_id)

    def _text(self, turn_id, text: str, frames: list[str]) -> list[str]:
        """Send `frames` (whose texts join to `text`) for `turn_id`, after the
        separator when this is the turn's first text and the task's previous
        text did not end in whitespace."""
        out: list[str] = []
        if turn_id not in self._live:
            prev = next(reversed(self._live.values()), "")
            if prev and not prev[-1].isspace():
                out.append(NEWLINE)
        self._live[turn_id] = (self._live.get(turn_id, "") + text)[-1:]
        return out + frames

    def _words(self, turn_id, text: str) -> list[str]:
        """A whole message, word-wise (the legacy shape)."""
        text = text.strip()
        return self._text(turn_id, text + " ", tokens(text)) if text else []

    # ------------------------------------------------------------ events
    def project(self, event: dict) -> list[str]:
        etype, body = event["type"], event["body"]
        if etype == "input":
            return [sse("seq", {"input_id": body["input_id"], "seq": event["seq"]})]
        if etype == "turn_start":
            if body.get("type") == "compaction":
                self._compaction.add(body["turn_id"])
            return []
        if etype == "llm_response":
            msg = body["message"]
            if body["turn_id"] in self._compaction:
                return []                       # a compaction summary is context, not an answer
            if body["finish_reason"] == "tool_calls" or msg.get("tool_calls"):
                return []
            if body["turn_id"] in self._live:
                return []                       # sent live, before the response
            return self._words(body["turn_id"], msg.get("content") or "")
        if etype == "tool_call":
            self._names[body["call_id"]] = body["name"]
            if body["side"] == "device":
                return [sse("tool_call", {"id": body["call_id"], "name": body["name"],
                                          "arguments": body["args"]})]
            return []
        if etype == "tool_result":
            call_id = body["call_id"]
            name = self._names.get(call_id)
            if name in AVATAR_TOOLS and not body.get("late"):
                raw = self._host_results.pop(call_id, body["content"])
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return []
                return [sse("avatar_command", {"name": name, "result": parsed})]
            return []
        if etype == "question":
            return [sse("question", {
                "question_id": body["question_id"], "call_id": body["call_id"],
                "text": body["text"], "options": body["options"],
                "deadline": body["deadline"],
            })]
        if etype == "error":
            if body["class"] == "fatal":
                return [sse("error", {"class": body["class"], "message": body["message"]})]
            return []
        if etype == "settle":
            out: list[str] = []
            if not self._live and body["answer"]:
                out.extend(tokens(body["answer"].strip()))
            self._live.clear()                  # turn_ids never recur across tasks
            self._compaction.clear()
            return out
        return []

    # ------------------------------------------------------------ live
    def live(self, msg: dict) -> list[str]:
        if msg.get("type") != "delta":
            return []
        body = msg.get("body", {})
        kind, text = body.get("kind"), body.get("text")
        if not isinstance(text, str) or not text:
            return []
        if kind == "content":
            return self._text(body.get("turn_id"), text, [sse("token", {"text": text})])
        if kind == "text":
            return self._words(body.get("turn_id"), text)
        return []                               # reasoning, tool_call: never shown


def project(event: dict) -> list[str]:
    return Projector().project(event)
