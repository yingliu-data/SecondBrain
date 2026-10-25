"""The conversation and the user memory changes of a session's event log, for the memory
index (issue #56, plan step 3)."""
from datetime import datetime, timezone

import pytest

from app.session.eventlog import memory_changes, session_turns
from app.session.session_dir import SessionDir
from ast_everything.memory import Turn
from eventlog_fakes import HASH, LogBuilder, store

TS = "2026-10-07T12:00:00.000Z"
AT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    return SessionDir.create(tmp_path / "u" / "t.s1", user_id="u", session_id="s1", tenant="t")


def said(session) -> list[tuple[str, str]]:
    return [(turn.said_by, turn.text) for turn in session_turns(session.root)]


def memory(log: LogBuilder, scope: str, op: str, slug: str, derived_from: str = "c1") -> None:
    log.emit("memory", {"scope": scope, "op": op, "slug": slug, "sha256": HASH, "derived_from": derived_from})


def compact(log: LogBuilder) -> None:
    """A compaction turn, its summary, and the session-scope memory file the summary is kept in."""
    through = len(log.events)
    log.emit("turn_start", {"turn_id": "c_1", "type": "compaction", "includes_through_seq": through,
                            "model": "m", "params": {}, "request_hash": HASH, "preamble_hash": HASH,
                            "workspace_tree_id": None})
    log.emit("llm_response", {"turn_id": "c_1", "finish_reason": "stop", "usage": {},
                              "message": {"role": "assistant", "content": "summary of the chat"}})
    log.emit("compaction", {"range": [1, through], "summary_turn_id": "c_1", "tokens_before": 9,
                            "tokens_after": 3})
    memory(log, "session", "write", "summary", derived_from="c_1")


def test_turns_in_order_across_runs(session):
    """Criterion 1: every input and answer of every run is a turn, numbered oldest first, at its event's ts."""
    store(session, LogBuilder().start().text_task("in_1", "hello", "hi there").events)
    store(session, LogBuilder().start().text_task("in_2", "again", "sure").events, new_run=True)
    assert session_turns(session.root) == [
        Turn(1, "user", AT, "hello"), Turn(2, "assistant", AT, "hi there"),
        Turn(3, "user", AT, "again"), Turn(4, "assistant", AT, "sure")]


def test_tool_call_responses_are_not_turns(session):
    """Criterion 3: a response that calls a tool is not a turn; the answer after the tool is."""
    store(session, LogBuilder().start().tool_task("in_1", "draft it", "drafted it").events)
    assert said(session) == [("user", "draft it"), ("assistant", "drafted it")]


def test_compaction_and_repeated_input_are_not_turns(session):
    """Criterion 3: a compaction summary and an input delivered again are not turns of the conversation."""
    log = LogBuilder().start().text_task("in_1", "hello", "hi there")
    log.emit("input", {"input_id": "in_1", "kind": "task", "text": "hello", "dedupe": "duplicate"})
    compact(log)
    store(session, log.events)
    assert said(session) == [("user", "hello"), ("assistant", "hi there")]


def test_memory_changes_are_the_last_user_op(session):
    """Criterion 4: the user-scope files the session wrote or removed, by their last op; a
    session-scope file, such as a compaction summary, is left out."""
    log = LogBuilder().start().tool_task("in_1", "remember this", "noted")
    memory(log, "user", "write", "editor")
    memory(log, "user", "write", "language")
    memory(log, "user", "remove", "editor")
    compact(log)
    store(session, log.events)
    assert memory_changes(session.root) == {"editor": "remove", "language": "write"}
