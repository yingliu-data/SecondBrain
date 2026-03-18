import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


@dataclass
class Email:
    message_id: str
    account: str
    folder: str
    from_addr: str
    to_addr: str
    subject: str
    date: str           # ISO date: YYYY-MM-DD
    body_preview: str
    raw_headers: dict
    importance: int = 0
    is_read: bool = False
    is_replied: bool = False
    db_id: Optional[int] = None


class EmailStore:
    """SQLite-backed local email cache with FTS5 full-text search."""

    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS emails (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id    TEXT NOT NULL,
                    account       TEXT NOT NULL,
                    folder        TEXT NOT NULL DEFAULT 'INBOX',
                    from_addr     TEXT DEFAULT '',
                    to_addr       TEXT DEFAULT '',
                    subject       TEXT DEFAULT '',
                    date          TEXT DEFAULT '',
                    body_preview  TEXT DEFAULT '',
                    raw_headers   TEXT DEFAULT '{}',
                    importance    INTEGER DEFAULT 0,
                    is_read       INTEGER DEFAULT 0,
                    is_replied    INTEGER DEFAULT 0,
                    fetched_at    TEXT DEFAULT (datetime('now')),
                    UNIQUE(message_id, account)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
                    from_addr, subject, body_preview,
                    content='emails', content_rowid='id'
                );

                CREATE TABLE IF NOT EXISTS email_sync_log (
                    account   TEXT PRIMARY KEY,
                    last_sync TEXT NOT NULL
                );
            """)

    def get_last_sync(self, account_name: str, initial_days: int = 30) -> datetime:
        """Return the datetime of the last sync for this account, or a default lookback."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_sync FROM email_sync_log WHERE account = ?",
                (account_name,),
            ).fetchone()
        if row:
            return datetime.fromisoformat(row["last_sync"]).replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - timedelta(days=initial_days)

    def update_last_sync(self, account_name: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_sync_log (account, last_sync) VALUES (?, ?)",
                (account_name, now),
            )

    def upsert_emails(self, emails: list) -> int:
        """Insert new emails, skip duplicates. Returns count of new rows inserted."""
        new_count = 0
        with self._connect() as conn:
            for em in emails:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO emails
                       (message_id, account, folder, from_addr, to_addr, subject,
                        date, body_preview, raw_headers, importance, is_read)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        em.message_id, em.account, em.folder, em.from_addr,
                        em.to_addr, em.subject, em.date, em.body_preview,
                        json.dumps(em.raw_headers), em.importance, int(em.is_read),
                    ),
                )
                if cur.rowcount:
                    conn.execute(
                        "INSERT INTO emails_fts(rowid, from_addr, subject, body_preview)"
                        " VALUES (?, ?, ?, ?)",
                        (cur.lastrowid, em.from_addr, em.subject, em.body_preview),
                    )
                    new_count += 1
        return new_count

    def get_important_unread(self, days_back: int = 7, max_results: int = 10) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM emails
                   WHERE importance > 0 AND is_read = 0 AND is_replied = 0
                     AND date >= ?
                   ORDER BY importance DESC, date DESC
                   LIMIT ?""",
                (cutoff, max_results),
            ).fetchall()
        return [self._row_to_email(r) for r in rows]

    def get_total_unread(self, days_back: int = 7) -> int:
        """Return total unread count (including junk) for the stats line."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM emails WHERE is_read = 0 AND date >= ?",
                (cutoff,),
            ).fetchone()
        return row["cnt"] if row else 0

    def search_fts(
        self, query: str, account: str = "all", days_back: int = 30, max_results: int = 10
    ) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            if account == "all":
                rows = conn.execute(
                    """SELECT e.* FROM emails e
                       JOIN emails_fts f ON e.id = f.rowid
                       WHERE emails_fts MATCH ?
                         AND e.date >= ?
                       ORDER BY e.date DESC
                       LIMIT ?""",
                    (query, cutoff, max_results),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT e.* FROM emails e
                       JOIN emails_fts f ON e.id = f.rowid
                       WHERE emails_fts MATCH ?
                         AND e.account = ?
                         AND e.date >= ?
                       ORDER BY e.date DESC
                       LIMIT ?""",
                    (query, account, cutoff, max_results),
                ).fetchall()
        return [self._row_to_email(r) for r in rows]

    def mark_replied(self, message_id: str, account: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE emails SET is_replied = 1 WHERE message_id = ? AND account = ?",
                (message_id, account),
            )

    def get_by_message_id(self, message_id: str, account: str) -> Optional[Email]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM emails WHERE message_id = ? AND account = ?",
                (message_id, account),
            ).fetchone()
        return self._row_to_email(row) if row else None

    def _row_to_email(self, row) -> Email:
        return Email(
            db_id=row["id"],
            message_id=row["message_id"],
            account=row["account"],
            folder=row["folder"],
            from_addr=row["from_addr"] or "",
            to_addr=row["to_addr"] or "",
            subject=row["subject"] or "",
            date=row["date"] or "",
            body_preview=row["body_preview"] or "",
            raw_headers=json.loads(row["raw_headers"] or "{}"),
            importance=row["importance"],
            is_read=bool(row["is_read"]),
            is_replied=bool(row["is_replied"]),
        )
