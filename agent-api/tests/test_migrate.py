import json
import sqlite3

import pytest

from protocol.canonical import derive_key_id
from protocol.fold import fold, replay
from protocol.replay import load_session, verify_session

from app.session.dir_store import DirStore
from app.session import migrate
from eventlog_fakes import conversation


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, messages TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    rows = [
        ("abc", json.dumps([{"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"}])),
        ("wcc-event:e1", json.dumps([{"role": "user", "content": "draft event"}])),
        ("bad:id:extra", json.dumps([])),  # session part "id:extra" is unsafe
    ]
    for sid, msgs in rows:
        conn.execute("INSERT INTO sessions (session_id, messages) VALUES (?, ?)", (sid, msgs))
    conn.commit()
    conn.close()


def test_migrate_and_idempotency(tmp_path, monkeypatch):
    db = tmp_path / "conversations.db"
    root = tmp_path / "sessions"
    _make_db(db)
    monkeypatch.setattr(migrate, "_tenant_users", lambda: {"wcc-event": "wcc", "default": "default"})

    stats = migrate.migrate(str(db), str(root))
    assert stats["migrated"] == 2
    assert stats["skipped_invalid"] == 1

    # the sqlite step writes the run itself: nothing left for --events
    assert migrate.migrate_events(root, quiet=True)["stats"] == {}
    store = DirStore(root)
    default_session = store.get("default", "default", "abc")
    assert default_session is not None
    history = conversation(default_session)
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "hi"), ("assistant", "hello")]

    wcc_session = store.get("wcc", "wcc-event", "e1")
    assert wcc_session is not None
    # new-layout directories only
    assert default_session.root == root / "default" / "default.abc"
    assert wcc_session.root == root / "wcc" / "wcc-event.e1"
    from app.session.dir_store import legacy_layout_dirs
    assert legacy_layout_dirs(root) == []
    assert wcc_session.read_meta()["tenant"] == "wcc-event"
    assert conversation(wcc_session) == [{"role": "user", "content": "draft event"}]
    assert verify_session(wcc_session.root) == []

    # Second run: everything already exists
    stats2 = migrate.migrate(str(db), str(root))
    assert stats2["migrated"] == 0
    assert stats2["skipped_existing"] == 2


# ---------------------------------------------------------------- --events

def _legacy(store, user="wcc", tenant="wcc-event", sid="sess_2026-05-01_101500_ab12",
            history=(), trace=()):
    """A session directory as the legacy engine left it: session.json,
    history.jsonl rows {ts, role, content, ticket_id}, logs/trace.jsonl."""
    session = store.get_or_create(user, tenant, sid)
    (session.root / migrate.HISTORY_FILE).write_text("".join(json.dumps(r) + "\n" for r in history))
    if trace:
        (session.root / migrate.TRACE_FILE).write_text("".join(json.dumps(r) + "\n" for r in trace))
    return session


REAL_SHAPED_HISTORY = [
    {"ts": "2026-05-01T10:15:02.117Z", "role": "user", "content": "Draft an event for Friday",
     "ticket_id": "tk_1a2b3c4d"},
    {"ts": "2026-05-01T10:15:09.804Z", "role": "assistant",
     "content": "I drafted **Friday Social** for 6pm.", "ticket_id": "tk_1a2b3c4d"},
    {"ts": "2026-05-01T10:16:40.001Z", "role": "user", "content": "Thanks! Make it 7pm",
     "ticket_id": "tk_5e6f7a8b"},
    {"ts": "2026-05-01T10:16:47.250Z", "role": "assistant", "content": "Updated to 7pm.",
     "ticket_id": "tk_5e6f7a8b"},
    {"ts": "2026-05-01T10:20:00.000Z", "role": "user", "content": "hello?",
     "ticket_id": "tk_99999999"},          # the client disconnected: never answered
]
REAL_SHAPED_TRACE = [
    {"ts": "2026-05-01T10:15:02.120Z", "event": "chat.turn.start", "ticket_id": "tk_1a2b3c4d"},
    {"ts": "2026-05-01T10:15:04.500Z", "event": "tool_call", "ticket_id": "tk_1a2b3c4d",
     "detail": {"name": "wcc-events__draft_event",
                "arguments": {"title": "Friday Social", "start": "18:00"}}},
    {"ts": "2026-05-01T10:15:05.900Z", "event": "tool_result", "ticket_id": "tk_1a2b3c4d",
     "detail": {"name": "wcc-events__draft_event", "result": "{\"id\": \"ev_1\"}",
                "duration_ms": 1400}},
    {"ts": "2026-05-01T10:15:09.810Z", "event": "assistant_response", "ticket_id": "tk_1a2b3c4d",
     "detail": {"chars": 36}},
    {"ts": "2026-05-01T10:15:09.900Z", "event": "chat.turn.success", "ticket_id": "tk_1a2b3c4d"},
    {"ts": "2026-05-01T10:16:42.000Z", "event": "tool_call", "ticket_id": "tk_5e6f7a8b",
     "detail": {"name": "wcc-events__update_event", "arguments": "{\"id\": \"ev_1\"}"}},
    {"ts": "2026-05-01T10:16:43.000Z", "event": "tool_result", "ticket_id": "tk_5e6f7a8b",
     "detail": {"name": "wcc-events__update_event", "result": "ok", "duration_ms": 1000}},
]


def _events(session):
    [run] = load_session(session.root)
    return run


def test_events_migration_of_a_real_shaped_session(tmp_path):
    store = DirStore(tmp_path / "sessions")
    sid = "sess_2026-05-01_101500_ab12"
    session = _legacy(store, sid=sid, history=REAL_SHAPED_HISTORY, trace=REAL_SHAPED_TRACE)

    result = migrate.migrate_events(store.root, quiet=True)
    assert result["stats"] == {"migrated": 1}
    report = result["sessions"][f"wcc/wcc-event.{sid}"]
    assert report["exchanges"] == 3 and report["replies"] == 2
    assert report["tool_calls"] == 2 and report["unanswered"] == 1

    assert sorted(p.name for p in (session.root / "events").iterdir()) == ["r0000"]
    assert verify_session(session.root) == []
    events = _events(session)
    state, problems = replay(events)
    assert problems == []

    start = events[0]
    assert start["type"] == "session_start" and start["session_id"] == sid
    assert start["body"]["migrated"] is True and start["body"]["resumed"] is False
    assert (start["body"]["user"], start["body"]["tenant"]) == ("wcc", "wcc-event")
    assert start["body"]["tools"] == [
        {"name": "wcc-events__draft_event", "side": "host", "pin": None},
        {"name": "wcc-events__update_event", "side": "host", "pin": None}]

    # inputs: task, id derived from the history line number
    inputs = [e["body"] for e in events if e["type"] == "input"]
    assert [i["text"] for i in inputs] == ["Draft an event for Friday", "Thanks! Make it 7pm",
                                           "hello?"]
    assert inputs[0]["input_id"] == derive_key_id("input", "wcc", "wcc-event", sid, "1")
    assert inputs[2]["input_id"] == derive_key_id("input", "wcc", "wcc-event", sid, "5")
    assert all(i["kind"] == "task" and i["dedupe"] == "new" for i in inputs)

    # settles: done with the reply, failed (empty) for the unanswered one
    settles = [e["body"] for e in events if e["type"] == "settle"]
    assert [(s["outcome"], s["answer"]) for s in settles] == [
        ("done", "I drafted **Friday Social** for 6pm."), ("done", "Updated to 7pm."),
        ("failed", "")]
    assert state.last_settle["outcome"] == "failed"

    # tool calls from the trace: tool_call{side: host} + op_state + tool_result
    calls = [e["body"] for e in events if e["type"] == "tool_call"]
    assert [(c["name"], c["side"], c["args"]) for c in calls] == [
        ("wcc-events__draft_event", "host", {"title": "Friday Social", "start": "18:00"}),
        ("wcc-events__update_event", "host", {"id": "ev_1"})]
    results = [e["body"] for e in events if e["type"] == "tool_result"]
    assert [r["content"] for r in results] == ["{\"id\": \"ev_1\"}", "ok"]
    ops = [e["body"] for e in events if e["type"] == "op_state"]
    assert [o["duration_ms"] for o in ops] == [1400, 1000]

    # clock events from the timestamps (data only); envelope ts from the rows
    clocks = [e for e in events if e["type"] == "clock"]
    assert clocks and all(c["body"]["kind"] == "turn_clock" for c in clocks)
    assert clocks[0]["body"]["at"] == 1777630504500       # 2026-05-01T10:15:04.500Z
    assert next(e for e in events if e["type"] == "tool_call")["ts"] == "2026-05-01T10:15:04.500Z"

    # the folded prefix is the conversation, tool rounds included, in order
    prefix = state.committed_prefix + [it["message"] for it in state.staged_suffix]
    assert [m["role"] for m in prefix] == [
        "user", "assistant", "tool", "assistant",
        "user", "assistant", "tool", "assistant", "user"]
    assert prefix[1]["tool_calls"][0]["function"]["name"] == "wcc-events__draft_event"
    assert prefix[2]["tool_call_id"] == prefix[1]["tool_calls"][0]["id"]

    # the session's history and checkpoint
    assert conversation(session)[:2] == [
        {"role": "user", "content": "Draft an event for Friday"},
        {"role": "assistant", "content": "I drafted **Friday Social** for 6pm."}]
    assert events[-1]["type"] == "checkpoint"
    assert events[-1]["body"]["through_seq"] == events[-2]["seq"]


def test_events_migration_dry_run_writes_nothing(tmp_path, capsys):
    store = DirStore(tmp_path / "sessions")
    session = _legacy(store, history=REAL_SHAPED_HISTORY, trace=REAL_SHAPED_TRACE)
    code = migrate.main(["--events", "--dry-run", str(store.root)])
    assert code == 0
    assert not (session.root / "events").exists()
    out = capsys.readouterr().out
    assert "would_migrate" in out and "dry run" in out
    assert '"tool_calls": 2' in out


def test_events_migration_is_idempotent(tmp_path):
    store = DirStore(tmp_path / "sessions")
    session = _legacy(store, history=REAL_SHAPED_HISTORY, trace=REAL_SHAPED_TRACE)
    assert migrate.main(["--events", str(store.root)]) == 0
    first = (session.root / "events" / "r0000" / "000001.jsonl").read_bytes()
    again = migrate.migrate_events(store.root, quiet=True)
    assert again["stats"] == {"skipped_existing": 1}
    assert (session.root / "events" / "r0000" / "000001.jsonl").read_bytes() == first
    # deterministic: the same legacy files give the same run byte for byte
    other = _legacy(DirStore(tmp_path / "other"), history=REAL_SHAPED_HISTORY,
                    trace=REAL_SHAPED_TRACE)
    migrate.migrate_events(tmp_path / "other", quiet=True)
    assert (other.root / "events" / "r0000" / "000001.jsonl").read_bytes() == first


def test_events_migration_history_tool_rows_and_segments(tmp_path):
    """Tool calls recorded in the history itself (assistant tool_calls + tool
    rows) win over the trace; segments follow the run layout; leading
    assistant rows and empty histories are skipped."""
    store = DirStore(tmp_path / "sessions")
    history = [
        {"ts": "2026-05-02T09:00:00.000Z", "role": "assistant", "content": "welcome"},
        {"ts": "2026-05-02T09:00:01.000Z", "role": "user", "content": "search x"},
        {"ts": "2026-05-02T09:00:02.000Z", "role": "assistant", "content": "",
         "tool_calls": [{"id": "call_9", "type": "function",
                         "function": {"name": "web_search", "arguments": "{\"q\": \"x\"}"}}]},
        {"ts": "2026-05-02T09:00:03.000Z", "role": "tool", "tool_call_id": "call_9",
         "content": "x is a letter"},
        {"ts": "2026-05-02T09:00:04.000Z", "role": "assistant", "content": "x is a letter."},
    ]
    session = _legacy(store, sid="s2", history=history)
    _legacy(store, sid="empty", history=())
    result = migrate.migrate_events(store.root, quiet=True, segment_events=4)
    assert result["stats"] == {"migrated": 1, "skipped_empty": 1}
    assert result["sessions"]["wcc/wcc-event.s2"]["skipped_rows"] == 1
    segments = sorted(p.name for p in (session.root / "events" / "r0000").iterdir())
    assert segments[0] == "000001.jsonl" and len(segments) > 1
    assert verify_session(session.root) == []
    events = _events(session)
    call = next(e["body"] for e in events if e["type"] == "tool_call")
    assert (call["call_id"], call["name"], call["args"]) == ("call_9", "web_search", {"q": "x"})
    assert not any(e["type"] == "op_state" for e in events)     # no duration recorded
    state = fold(events)
    assert state.open_calls["call_9"]["state"] == "done"


@pytest.mark.no_log_verify("an empty run directory stands in for a live session's run")
def test_events_migration_skips_sessions_with_a_run(tmp_path):
    store = DirStore(tmp_path / "sessions")
    session = _legacy(store, history=REAL_SHAPED_HISTORY)
    (session.root / "events" / "r0001").mkdir(parents=True)
    assert migrate.migrate_events(store.root, quiet=True)["stats"] == {"skipped_existing": 1}
    assert [p.name for p in (session.root / "events").iterdir()] == ["r0001"]


def test_events_migration_finishes_an_interrupted_one(tmp_path):
    """An `events/` without a run (a migration interrupted before its
    rename, its temporary directory left behind) is migrated again."""
    store = DirStore(tmp_path / "sessions")
    session = _legacy(store, history=REAL_SHAPED_HISTORY)
    (session.root / "events" / ".r0000.new").mkdir(parents=True)
    assert migrate.migrate_events(store.root, quiet=True)["stats"] == {"migrated": 1}
    assert [p.name for p in (session.root / "events").iterdir()] == ["r0000"]
    assert verify_session(session.root) == []


def _purge_tree(store):
    from app.session.session_dir import SessionDir
    user_dir = store.root / "u"
    old = SessionDir.create(user_dir / "wcc_event_s1", user_id="u", session_id="s1",
                            tenant="wcc_event")
    (old.workspace / "keep.txt").write_text("a" * 100)
    SessionDir.create(user_dir / "wcc_x", user_id="u", session_id="x", tenant="wcc")
    new = store.get_or_create("u", "wcc_event", "s1")         # the new layout
    (new.workspace / "keep.txt").write_text("b")
    (user_dir / "scratch").mkdir()                            # no session.json
    (user_dir / "scratch" / "f").write_text("c")
    (user_dir / "loose_file").write_text("d")                 # not a directory
    return user_dir


def test_purge_legacy_layout_dry_run_lists_without_deleting(tmp_path, capsys):
    store = DirStore(tmp_path / "sessions")
    user_dir = _purge_tree(store)
    before = sorted(p.name for p in user_dir.iterdir())
    result = migrate.purge_legacy_layout(store.root, dry_run=True)
    assert sorted(result["dirs"]) == ["u/wcc_event_s1", "u/wcc_x"]
    assert result["dirs"]["u/wcc_event_s1"] >= 100 and result["deleted"] is False
    out = capsys.readouterr().out
    assert "would delete u/wcc_event_s1" in out and "bytes" in out
    assert migrate.main(["--purge-legacy-layout", "--dry-run", str(store.root)]) == 0
    assert sorted(p.name for p in user_dir.iterdir()) == before


def test_purge_legacy_layout_deletes_only_old_layout_dirs(tmp_path):
    store = DirStore(tmp_path / "sessions")
    user_dir = _purge_tree(store)
    assert migrate.main(["--purge-legacy-layout", str(store.root)]) == 0
    assert sorted(p.name for p in user_dir.iterdir()) == [
        "loose_file", "scratch", "wcc_event.s1"]
    assert (user_dir / "scratch" / "f").read_text() == "c"
    assert (store.get("u", "wcc_event", "s1").workspace / "keep.txt").read_text() == "b"
    again = migrate.purge_legacy_layout(store.root, quiet=True)   # idempotent
    assert again["dirs"] == {}


def test_purge_legacy_layout_needs_an_explicit_root():
    with pytest.raises(SystemExit):
        migrate.main(["--purge-legacy-layout"])


def test_events_migration_skips_old_layout_dirs(tmp_path):
    """--events writes new-layout directories only: an old-layout directory
    with a history.jsonl is left alone (purge it instead)."""
    from app.session.session_dir import SessionDir
    store = DirStore(tmp_path / "sessions")
    old = SessionDir.create(store.root / "wcc" / "wcc-event_s9", user_id="wcc",
                            session_id="s9", tenant="wcc-event")
    (old.root / migrate.HISTORY_FILE).write_text(
        "".join(json.dumps(r) + "\n" for r in REAL_SHAPED_HISTORY))
    session = _legacy(store, history=REAL_SHAPED_HISTORY)
    result = migrate.migrate_events(store.root, quiet=True)
    assert result["stats"] == {"migrated": 1}
    assert list(result["sessions"]) == [str(session.root.relative_to(store.root))]
    assert not (old.root / "events").exists() or not any((old.root / "events").iterdir())
