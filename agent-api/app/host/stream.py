"""stream — guest events → the SSE strings the clients already consume.

The strings are byte-identical to app/agent/loop.py's: `token` payloads are
`json.dumps({'text': word + ' '})` word by word (the iPhone's TTS expects
the word-wise emission), `tool_call` is `{id, name, arguments}`,
`avatar_command` is `{name, result}`, `done` is `{}`. Added for the harness:
`seq{input_id, seq}` on every input, `question{...}` and `error{...}`.

  Projector()                 per session; remembers call_id → tool name and
                              whether a task's text was already streamed
    .project(event) -> [sse]  one stored event
    .live(msg) -> [sse]       one live message (delta → token)
    .note_host_result(call_id, content)
                              the verbatim host-skill result (the guest may
                              receive a compacted copy, e.g. plan_movement)
  project(event) -> [sse]     stateless convenience (a fresh Projector)
  sse(event, data) -> str     the one formatter
  KEEPALIVE                   ": keepalive\\n\\n", every 15 s while a task runs
"""
from __future__ import annotations

import json

from app.agent.loop import _AVATAR_TOOLS

KEEPALIVE = ": keepalive\n\n"
AVATAR_TOOLS = _AVATAR_TOOLS


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def tokens(text: str) -> list[str]:
    """Word-wise token events, exactly as the legacy loop streams a reply."""
    return [sse("token", {"text": word + " "}) for word in text.split(" ")]


DONE = "event: done\ndata: {}\n\n"


class Projector:
    def __init__(self) -> None:
        self._names: dict[str, str] = {}        # call_id -> tool name
        self._sides: dict[str, str] = {}
        self._host_results: dict[str, str] = {}
        self._streamed = False                  # text already sent for this task
        self._deltas: set[str] = set()          # turn_ids that streamed deltas

    def note_host_result(self, call_id: str, content: str) -> None:
        self._host_results[call_id] = content

    def tool_name(self, call_id: str) -> str | None:
        return self._names.get(call_id)

    # ------------------------------------------------------------ events
    def project(self, event: dict) -> list[str]:
        etype, body = event["type"], event["body"]
        if etype == "input":
            if body["kind"] != "ui_response" and body["dedupe"] == "new":
                self._streamed = False
            return [sse("seq", {"input_id": body["input_id"], "seq": event["seq"]})]
        if etype == "llm_response":
            msg = body["message"]
            if body["finish_reason"] == "tool_calls" or msg.get("tool_calls"):
                return []
            if body["turn_id"] in self._deltas:
                return []
            text = (msg.get("content") or "").strip()
            if not text:
                return []
            self._streamed = True
            return tokens(text)
        if etype == "tool_call":
            self._names[body["call_id"]] = body["name"]
            self._sides[body["call_id"]] = body["side"]
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
            if not self._streamed and body["answer"]:
                out.extend(tokens(body["answer"].strip()))
            self._streamed = False
            out.append(DONE)
            return out
        return []

    # ------------------------------------------------------------ live
    def live(self, msg: dict) -> list[str]:
        if msg.get("type") != "delta":
            return []
        body = msg.get("body", {})
        if body.get("kind", "text") != "text":
            return []
        text = body.get("text", "")
        if not text:
            return []
        turn_id = body.get("turn_id")
        if turn_id:
            self._deltas.add(turn_id)
        self._streamed = True
        return [sse("token", {"text": text})]


def project(event: dict) -> list[str]:
    return Projector().project(event)
