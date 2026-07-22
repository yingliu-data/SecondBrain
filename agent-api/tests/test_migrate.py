import json
import sqlite3

from app.session.dir_store import DirStore
from app.session import migrate


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

    store = DirStore(root)
    default_session = store.get("default", "default", "abc")
    assert default_session is not None
    history = default_session.read_history()
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "hi"), ("assistant", "hello")]

    wcc_session = store.get("wcc", "wcc-event", "e1")
    assert wcc_session is not None
    assert wcc_session.read_meta()["tenant"] == "wcc-event"
    assert any(t["event"] == "migrated_from_sqlite" for t in wcc_session.read_trace())

    # Second run: everything already exists
    stats2 = migrate.migrate(str(db), str(root))
    assert stats2["migrated"] == 0
    assert stats2["skipped_existing"] == 2
