"""outcomes — what both drivers share about a task's end and its ids
(docs/HOST.md): the settle outcomes, a finished task's outcome and error,
the ticket id of an input and the input id of an answer.
"""
from __future__ import annotations

import asyncio
import uuid

from protocol.canonical import derive_key_id
from protocol.events import ENUMS

# the guest's settle outcomes (anything else a task returns counts as failed)
SETTLE_OUTCOMES = ENUMS["settle"]["outcome"]
# the drivers of a task (task_input["driver"]): act() and the workflow's run_task
DIRECT, WORKFLOW = "direct", "workflow"


class GuestError(RuntimeError):
    """The guest died, broke the protocol, or never connected."""


def new_input_id() -> str:
    return "in_" + uuid.uuid4().hex


def answer_input_id(input_id: str, question_id: str) -> str:
    """The ui_response's input_id of an answer to `question_id` asked by
    the task of `input_id`, on either path: stable across retries and
    re-sent answers, so the guest dedupes a second one."""
    return f"{input_id}:answer:{question_id}"


def ticket_id_for(ref, input_id: str) -> str:
    """The ticket of one input: `ticket_` + 24 hex of the canonical hash
    of (user, tenant, session_id, input_id). Not the settle seq: seqs
    restart with every guest run."""
    return derive_key_id("ticket", ref.user, ref.tenant, ref.session_id, input_id)


def task_ok(t: asyncio.Future) -> bool:
    """A task that finished with a result (not cancelled, no exception)."""
    return t.done() and not t.cancelled() and t.exception() is None


def task_error(t: asyncio.Future | None) -> str | None:
    """The message of a finished task's host failure: its _fail result's
    `host_error`, or the exception it raised; None for a settle."""
    if t is None or not t.done() or t.cancelled():
        return None
    if t.exception() is not None:
        return str(t.exception()) or type(t.exception()).__name__
    return t.result().get("host_error")


def settle_outcome(outcome) -> str:
    """`outcome` when it is a settle outcome (ENUMS), else failed: how both
    drivers normalise a task's outcome (act()'s streams, RunResult)."""
    return outcome if outcome in SETTLE_OUTCOMES else "failed"


def task_outcome(t: asyncio.Future) -> str:
    """A finished task's outcome: its result's, else failed."""
    return settle_outcome(t.result().get("outcome")) if task_ok(t) else "failed"


def end_error(t: asyncio.Future | None, outcome: str) -> str | None:
    """The SSE error that ends an input's stream (finish_input), on either
    driver: none for done, else the host failure or the outcome's name."""
    if outcome == "done":
        return None
    return task_error(t) or f"The task ended: {outcome}."
