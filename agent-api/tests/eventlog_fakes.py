"""Harness event logs for tests that read sessions (traces, history):
a text task and a host-tool task, stored through StateSync like a live run."""
from __future__ import annotations

from app.host.state_sync import StateSync

HASH = "0" * 64
TOOLS = [{"name": "bash", "side": "guest", "pin": None},
         {"name": "wcc-events__draft_event", "side": "host", "pin": None}]


class LogBuilder:
    def __init__(self, session_id: str = "s1", ts: str = "2026-10-07T12:00:00.000Z",
                 user: str = "u", tenant: str = "t"):
        self.session_id, self.ts, self.user, self.tenant = session_id, ts, user, tenant
        self.events: list[dict] = []

    def emit(self, type: str, body: dict, ts: str | None = None) -> dict:
        ev = {"session_id": self.session_id, "seq": len(self.events) + 1, "ts": ts or self.ts,
              "schema": 1, "type": type, "body": body}
        self.events.append(ev)
        return ev

    def start(self) -> "LogBuilder":
        self.emit("session_start", {
            "user": self.user, "tenant": self.tenant, "workflow_run_id": "w", "config_hash": HASH,
            "system_hash": HASH, "tools": TOOLS, "harness_version": "x", "image_version": "x",
            "resumed": False})
        return self

    def turn(self, turn_id: str, message: dict, finish_reason: str = "stop") -> None:
        self.emit("clock", {"kind": "turn_clock", "at": 1})
        self.emit("turn_start", {"turn_id": turn_id, "type": "regular",
                                 "includes_through_seq": len(self.events),
                                 "model": "m", "params": {}, "request_hash": HASH,
                                 "preamble_hash": HASH, "workspace_tree_id": None})
        self.emit("llm_response", {"turn_id": turn_id, "finish_reason": finish_reason,
                                   "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                                   "message": {"role": "assistant", **message}})

    def text_task(self, input_id: str, text: str, answer: str) -> "LogBuilder":
        self.emit("input", {"input_id": input_id, "kind": "task", "text": text, "dedupe": "new"})
        self.turn(f"t_{input_id}", {"content": answer})
        self.settle(input_id, answer)
        return self

    def tool_task(self, input_id: str, text: str, answer: str, *, call_id: str = "c1",
                  name: str = "wcc-events__draft_event", duration_ms: int = 42) -> "LogBuilder":
        self.emit("input", {"input_id": input_id, "kind": "task", "text": text, "dedupe": "new"})
        self.turn(f"t1_{input_id}", {"content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}]}, "tool_calls")
        self.emit("tool_call", {"turn_id": f"t1_{input_id}", "call_id": call_id, "name": name,
                                "side": "host", "args": {"title": "x"}, "state": "RUNNING"})
        self.emit("op_state", {"call_id": call_id, "state": "done", "duration_ms": duration_ms})
        self.emit("tool_result", {"call_id": call_id, "content": "drafted"})
        self.turn(f"t2_{input_id}", {"content": answer})
        self.settle(input_id, answer)
        return self

    def settle(self, input_id: str, answer: str, outcome: str = "done") -> None:
        self.emit("settle", {"input_id": input_id, "outcome": outcome, "answer": answer,
                             "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                             "manifest": {}})


def store(session, events: list[dict], *, new_run: bool = False) -> StateSync:
    """Accept `events` into the session's event log (a new run when asked)."""
    sync = StateSync(session)
    if new_run:
        sync.begin_run()
    for ev in events:
        sync.accept(ev)
    return sync
