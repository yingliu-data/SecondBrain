"""logbuilder — the one event-log builder the tests and the golden generator share.

`Log(session_id, tools, ts)` appends events with a gapless seq. `ts` is a
callable seq -> str so a caller can pin the exact timestamp text (the goldens
freeze "2026-10-07T12:00:00.<seq:03d>Z"); `ts` is data, the fold never reads it.
Every per-type helper takes `**overrides` merged into the body, so a test can
bend one field without rebuilding the whole event. Standard library only.
"""
from __future__ import annotations

import json
from typing import Callable

from protocol.canonical import hash_value
from protocol.events import make_event
from protocol.fold import fold, state_hash

SID = "sess-test"
TOOLS = [
    {"name": "bash", "side": "guest", "pin": None},
    {"name": "question", "side": "guest", "pin": None},
    {"name": "calendar", "side": "device", "pin": None},
    {"name": "reminders", "side": "device", "pin": None},
]


def default_ts(seq: int) -> str:
    return f"2026-01-01T00:00:{seq % 60:02d}Z"


class Log:
    """Appends events with a gapless seq and a fixed ts."""

    def __init__(self, session_id: str = SID, tools=TOOLS,
                 ts: Callable[[int], str] = default_ts):
        self.session_id = session_id
        self.tools = tools
        self.ts = ts
        self.events: list[dict] = []

    @property
    def next_seq(self) -> int:
        return len(self.events) + 1

    def add(self, type: str, body: dict, **envelope) -> dict:
        seq = self.next_seq
        ev = make_event(self.session_id, seq, self.ts(seq), type, body)
        ev.update(envelope)
        self.events.append(ev)
        return ev

    # ------------------------------------------------------------ per type
    def start(self, **overrides) -> dict:
        return self.add("session_start", {
            "user": "u", "tenant": "t", "workflow_run_id": "u/t/" + self.session_id,
            "config_hash": "c" * 64, "system_hash": "s" * 64, "tools": self.tools,
            "harness_version": "0.0.1", "image_version": "test", "resumed": False,
            **overrides,
        })

    def input(self, input_id, text, kind="task", dedupe="new", **overrides) -> dict:
        return self.add("input", {"input_id": input_id, "kind": kind, "text": text,
                                  "dedupe": dedupe, **overrides})

    def clock(self, at, kind="turn_clock") -> dict:
        return self.add("clock", {"kind": kind, "at": at})

    def turn(self, turn_id, includes=None, type="regular", preamble="p1", **overrides) -> dict:
        return self.add("turn_start", {
            "turn_id": turn_id, "type": type,
            "includes_through_seq": self.next_seq - 1 if includes is None else includes,
            "model": "m", "params": {}, "request_hash": "r" * 64,
            "preamble_hash": preamble, "workspace_tree_id": None,
            **overrides,
        })

    def response(self, turn_id, content=None, calls=(), usage=(10, 5), **overrides) -> dict:
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [
                {"id": cid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for cid, name, args in calls]
        return self.add("llm_response", {
            "turn_id": turn_id, "message": message,
            "finish_reason": "tool_calls" if calls else "stop",
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
            **overrides,
        })

    def call(self, turn_id, call_id, name, side, args=None, **overrides) -> dict:
        return self.add("tool_call", {"turn_id": turn_id, "call_id": call_id, "name": name,
                                      "side": side, "args": args or {}, "state": "RUNNING",
                                      **overrides})

    def result(self, call_id, content, **overrides) -> dict:
        return self.add("tool_result", {"call_id": call_id, "content": content, **overrides})

    def settle(self, input_id, outcome="done", answer="ok", **overrides) -> dict:
        return self.add("settle", {"input_id": input_id, "outcome": outcome, "answer": answer,
                                   "usage": {}, "manifest": {}, **overrides})

    def checkpoint(self) -> dict:
        """A checkpoint through everything so far: state_hash from the fold,
        prev_hash = hash_value(session_start) first, then the previous one's."""
        through = self.next_seq - 1
        prev = [e for e in self.events if e["type"] == "checkpoint"]
        prev_hash = prev[-1]["body"]["state_hash"] if prev else hash_value(self.events[0])
        return self.add("checkpoint", {
            "through_seq": through,
            "state_hash": state_hash(fold(self.events, upto_seq=through)),
            "prev_hash": prev_hash,
        })
