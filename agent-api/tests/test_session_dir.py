from datetime import datetime, timedelta, timezone

import pytest

from app.session.manifest import Manifest
from app.session.session_dir import SessionDir
from app.session.state import StateError
from app.session.ticket import Ticket
from eventlog_fakes import LogBuilder, store


@pytest.fixture
def session(tmp_path):
    return SessionDir.create(tmp_path / "u_s1", user_id="u", session_id="s1", tenant="t")


def test_create_layout_and_meta(session):
    for sub in ("workspace", "memory", "ipc", "tickets", "logs"):
        assert (session.root / sub).is_dir()
    meta = session.read_meta()
    assert meta["state"] == "active"
    assert meta["tenant"] == "t"
    assert meta["schema_version"] == 1
    loaded = SessionDir.load(session.root)
    assert loaded.read_meta()["session_id"] == "s1"


def test_load_rejects_non_session(tmp_path):
    with pytest.raises(FileNotFoundError):
        SessionDir.load(tmp_path / "nope")


def test_state_forward_only(session):
    session.set_state("complete")
    assert session.read_meta()["state"] == "complete"
    with pytest.raises(StateError):
        session.set_state("active")  # terminal states have no exits


def test_history_comes_from_the_event_log(session):
    """No history.jsonl; read_history folds every run's inputs + settles."""
    store(session, LogBuilder().start().text_task("in_1", "hello", "hi there").events)
    store(session, LogBuilder().start().text_task("in_2", "again", "sure").events, new_run=True)
    msgs = session.read_history()
    assert msgs == [{"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi there"},
                    {"role": "user", "content": "again"},
                    {"role": "assistant", "content": "sure"}]
    assert session.read_history(limit=1) == [{"role": "assistant", "content": "sure"}]
    assert not session.history_jsonl.exists()


def test_trace_from_the_event_log_and_since(session):
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    store(session, LogBuilder(ts=now).start().tool_task("in_1", "go", "done").events)
    rows = session.read_trace()
    assert [r["event"] for r in rows] == [
        "turn_start", "llm_response", "tool_call", "op_state", "tool_result",
        "turn_start", "assistant_response", "settle"]
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert session.read_trace(since=future) == []
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert len(session.read_trace(since=past)) == 8
    assert [r["event"] for r in session.read_trace(limit=2)] == ["assistant_response", "settle"]
    assert not session.trace_jsonl.exists()


def test_ticket_lifecycle(session):
    import json
    ticket = Ticket.start(session, "chat.turn", inputs={"message": "hi"})
    record = json.loads(ticket.path.read_text())
    assert record["state"] == "running"
    assert record["inputs_hash"].startswith("sha256:")
    ticket.finish("success", summary="done")
    record = json.loads(ticket.path.read_text())
    assert record["state"] == "success"
    assert record["completed_at"]
    assert session.read_meta()["last_ticket_id"] == ticket.ticket_id
    assert not session.trace_jsonl.exists()      # the ticket file is the record


def test_manifest_detects_tamper(session):
    f = session.workspace / "out.txt"
    f.write_text("original")
    manifest = Manifest.compute(session.root, ["workspace/out.txt"])
    assert manifest.verify(session.root) == []
    f.write_text("tampered")
    assert manifest.verify(session.root) == ["workspace/out.txt"]
