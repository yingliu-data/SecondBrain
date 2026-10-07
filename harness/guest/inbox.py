"""inbox — classify a host control message as the `input` event it becomes
(PLAN.md section 5 "input" row, invariant 4 of docs/REPLAY.md; docs/GUEST.md).

  Inbox(state).classify(control_msg) -> (dedupe, input_body)

`dedupe` is "new", "duplicate" or "late":
  duplicate  the input_id is already in state.seen_input_ids (a redelivered
             POST: recorded, never acted on)
  late       a ui_response for a question that is unknown, already answered,
             or whose deadline is behind the fold's clock (state.clock_at, ms)
  new        everything else

`input_body` is the body of the `input` event the coordinator emits:
{input_id, kind, text, dedupe} plus `attachments` for task/follow_up/steer
(recalled memories, computed on the host; always a list) and
`question_id`, `answer` for ui_response.

Pure: reads the State, never mutates it, never reads the clock.
"""
from __future__ import annotations

import json

INPUT_KINDS = frozenset({"task", "follow_up", "steer", "ui_response"})


def _str(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: expected non-empty string, got {value!r}")
    return value


class Inbox:
    def __init__(self, state):
        self.state = state

    def classify(self, msg: dict, at: int | None = None) -> tuple[str, dict]:
        """`msg` is a control message (or just its {type, body}); `at` overrides
        the fold's clock for the deadline check (the host's own `at` when the
        coordinator has it)."""
        if not isinstance(msg, dict):
            raise ValueError(f"control message must be an object, got {type(msg).__name__}")
        kind = msg.get("type")
        if kind not in INPUT_KINDS:
            raise ValueError(f"not an input control message: {kind!r}")
        body = msg.get("body")
        if not isinstance(body, dict):
            raise ValueError("control message body must be an object")
        input_id = _str(body.get("input_id"), f"{kind}.input_id")

        if kind == "ui_response":
            return self._ui_response(input_id, body, at)

        text = body.get("text", "")
        if not isinstance(text, str):
            raise ValueError(f"{kind}.text: expected string, got {type(text).__name__}")
        attachments = body.get("attachments", [])
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list):
            raise ValueError(f"{kind}.attachments: expected list, got {type(attachments).__name__}")
        dedupe = "duplicate" if input_id in self.state.seen_input_ids else "new"
        return dedupe, {
            "input_id": input_id, "kind": kind, "text": text, "dedupe": dedupe,
            "attachments": attachments,
        }

    def _ui_response(self, input_id: str, body: dict, at: int | None) -> tuple[str, dict]:
        qid = _str(body.get("question_id"), "ui_response.question_id")
        answer = body.get("answer")
        text = answer if isinstance(answer, str) else json.dumps(answer, sort_keys=True)
        if input_id in self.state.seen_input_ids:
            dedupe = "duplicate"
        else:
            dedupe = "new" if self._question_open(qid, at) else "late"
        return dedupe, {
            "input_id": input_id, "kind": "ui_response", "text": text, "dedupe": dedupe,
            "question_id": qid, "answer": text,
        }

    def _question_open(self, qid: str, at: int | None) -> bool:
        q = self.state.open_questions.get(qid)
        if q is None or q.get("answered"):
            return False
        now = at if at is not None else self.state.clock_at
        return now is None or now <= q["deadline"]
