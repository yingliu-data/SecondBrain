"""Per-operation ticket records.

One ticket per operation (``chat.turn``, ``llm.chat_completion``, ``tool.X``).
Tickets are append-only: ``start()`` writes the open record, ``finish()``
rewrites it atomically with the final state / summary / manifest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.session.ids import make_ticket_id
from app.session.manifest import Manifest
from app.session.session_dir import SessionDir, utcnow_iso
from app.util.ipc import atomic_write_json


def _inputs_hash(inputs: Any) -> str:
    raw = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Ticket:
    def __init__(self, session: SessionDir, ticket_id: str, path: Path, record: dict[str, Any]):
        self.session = session
        self.ticket_id = ticket_id
        self.path = path
        self.record = record

    @classmethod
    def start(
        cls,
        session: SessionDir,
        operation: str,
        *,
        inputs: Any | None = None,
    ) -> "Ticket":
        ticket_id = make_ticket_id()
        now = utcnow_iso()
        record: dict[str, Any] = {
            "ticket_id": ticket_id,
            "operation": operation,
            "state": "running",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "summary": "",
            "inputs_hash": _inputs_hash(inputs) if inputs is not None else None,
            "manifest": {"files": [], "sha256": {}},
        }
        path = session.tickets / f"{ticket_id}.json"
        atomic_write_json(path, record)
        session.update_meta(last_ticket_id=ticket_id)
        session.append_trace(f"{operation}.start", ticket_id=ticket_id)
        return cls(session, ticket_id, path, record)

    def finish(
        self,
        state: str,
        *,
        summary: str = "",
        manifest: Manifest | None = None,
    ) -> None:
        if state not in ("success", "failed"):
            raise ValueError(f"Ticket state must be 'success' or 'failed', got {state!r}")
        now = utcnow_iso()
        self.record.update(
            {
                "state": state,
                "summary": summary,
                "updated_at": now,
                "completed_at": now,
            }
        )
        if manifest is not None:
            self.record["manifest"] = manifest.to_dict()
        atomic_write_json(self.path, self.record)
        self.session.append_trace(
            f"{self.record['operation']}.{state}",
            {"summary": summary} if summary else None,
            ticket_id=self.ticket_id,
        )
