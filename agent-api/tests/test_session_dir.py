from datetime import datetime, timedelta, timezone

import pytest

from app.session.manifest import Manifest
from app.session.session_dir import SessionDir
from app.session.state import StateError
from app.session.ticket import Ticket


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


def test_history_round_trip(session):
    session.append_history("user", "hello")
    session.append_history("assistant", "hi there")
    msgs = session.read_history()
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "hello"), ("assistant", "hi there")]
    assert session.read_meta()["message_count"] == 2


def test_trace_append_and_read_since(session):
    session.append_trace("tool_call", {"name": "x"})
    session.append_trace("tool_result", {"name": "x", "duration_ms": 12})
    rows = session.read_trace()
    assert [r["event"] for r in rows] == ["tool_call", "tool_result"]
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert session.read_trace(since=future) == []
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert len(session.read_trace(since=past)) == 2


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
    events = [r["event"] for r in session.read_trace()]
    assert "chat.turn.start" in events and "chat.turn.success" in events


def test_manifest_detects_tamper(session):
    f = session.workspace / "out.txt"
    f.write_text("original")
    manifest = Manifest.compute(session.root, ["workspace/out.txt"])
    assert manifest.verify(session.root) == []
    f.write_text("tampered")
    assert manifest.verify(session.root) == ["workspace/out.txt"]
