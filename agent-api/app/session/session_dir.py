"""SessionDir — wraps one session's directory with file I/O.

Layout::

    {sessions_root}/{user}/{tenant}.{session_id}/      (app.session.dir_store)
        session.json         # metadata + forward-only state
        events/              # the event log (canonical): rNNNN/ runs, host-*.jsonl
        workspace/           # agent-produced files
        memory/              # durable facts
        ipc/                 # atomic-rename handoff (Phase 3)
        tickets/             # append-only per-operation records (Phase 3)
        logs/

The trace is read from the event log (app.session.eventlog); the legacy
`history.jsonl` / `logs/trace.jsonl` files are no longer written. A legacy
session's files are converted to an event log run by
`python -m app.session.migrate --events`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from protocol.clock import now_iso
from protocol.fsio import atomic_write_json

from app.session.eventlog import read_trace
from app.session.state import transition

SCHEMA_VERSION = 1
_SUBDIRS = ("workspace", "memory", "ipc", "tickets", "logs")


class SessionDir:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def session_json(self) -> Path:
        return self.root / "session.json"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def memory(self) -> Path:
        return self.root / "memory"

    @property
    def ipc(self) -> Path:
        return self.root / "ipc"

    @property
    def tickets(self) -> Path:
        return self.root / "tickets"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        user_id: str,
        session_id: str,
        tenant: str = "",
        title: str = "",
    ) -> "SessionDir":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
        now = now_iso()
        meta = {
            "session_id": session_id,
            "user_id": user_id,
            "tenant": tenant,
            "state": "active",
            "created_at": now,
            "updated_at": now,
            "title": title,
            "last_ticket_id": None,
            "schema_version": SCHEMA_VERSION,
        }
        sd = cls(root)
        sd._write_meta(meta)
        return sd

    @classmethod
    def load(cls, root: Path) -> "SessionDir":
        root = Path(root)
        if not (root / "session.json").exists():
            raise FileNotFoundError(f"Not a session dir: {root}")
        return cls(root)

    def read_meta(self) -> dict[str, Any]:
        return json.loads(self.session_json.read_text())

    def update_meta(self, **changes: Any) -> dict[str, Any]:
        meta = self.read_meta()
        meta.update(changes)
        meta["updated_at"] = now_iso()
        self._write_meta(meta)
        return meta

    def _write_meta(self, meta: dict[str, Any]) -> None:
        # durable=False: as before, session.json is renamed into place without
        # fsyncs (metadata the event log does not depend on); 0644 as before
        atomic_write_json(self.session_json, meta, durable=False, mode=0o644, indent=2)

    def set_state(self, new_state: str) -> None:
        meta = self.read_meta()
        current = meta.get("state", "active")
        transition(current, new_state)
        if new_state != current:
            self.update_meta(state=new_state)

    @property
    def events(self) -> Path:
        return self.root / "events"

    def read_trace(
        self,
        *,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Trace rows built from the event log (every run), oldest first;
        optionally only those at or after `since` (eventlog.read_trace)."""
        return read_trace(self.root, since=since, limit=limit)
