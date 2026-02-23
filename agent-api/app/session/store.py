import sqlite3
import json
import threading
from pathlib import Path


class SessionStore:
    """SQLite-backed session store with dict-like interface."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                messages   TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()

    def get(self, session_id: str) -> list:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT messages FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions (session_id, messages) VALUES (?, '[]')",
                (session_id,),
            )
            conn.commit()
            return []
        return json.loads(row[0])

    def save(self, session_id: str, messages: list):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO sessions (session_id, messages, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(session_id) DO UPDATE SET
                   messages = excluded.messages,
                   updated_at = excluded.updated_at""",
            (session_id, json.dumps(messages)),
        )
        conn.commit()

    def delete(self, session_id: str) -> bool:
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        return cursor.rowcount > 0

    def list_sessions(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT session_id, updated_at FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [{"session_id": r[0], "updated_at": r[1]} for r in rows]

    def setdefault(self, session_id: str, default: list) -> list:
        return self.get(session_id)
