"""SessionDir — wraps one session's directory with file I/O.

Layout::

    {root}/{user_id}_{session_id}/
        session.json         # metadata + forward-only state
        history.jsonl        # append-only LLM conversation log
        workspace/           # agent-produced files
        memory/              # durable facts
        ipc/                 # atomic-rename handoff (Phase 3)
        tickets/             # append-only per-operation records (Phase 3)
        logs/trace.jsonl     # append-only event stream
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.session.state import transition

SCHEMA_VERSION = 1
_SUBDIRS = ("workspace", "memory", "ipc", "tickets", "logs")


def utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class SessionDir:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def session_json(self) -> Path:
        return self.root / "session.json"

    @property
    def history_jsonl(self) -> Path:
        return self.root / "history.jsonl"

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

    @property
    def trace_jsonl(self) -> Path:
        return self.logs / "trace.jsonl"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        user_id: str,
        session_id: str,
        title: str = "",
    ) -> "SessionDir":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
        (root / "history.jsonl").touch(exist_ok=True)
        (root / "logs" / "trace.jsonl").touch(exist_ok=True)
        now = utcnow_iso()
        meta = {
            "session_id": session_id,
            "user_id": user_id,
            "state": "active",
            "created_at": now,
            "updated_at": now,
            "title": title,
            "message_count": 0,
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
        meta["updated_at"] = utcnow_iso()
        self._write_meta(meta)
        return meta

    def _write_meta(self, meta: dict[str, Any]) -> None:
        _atomic_write_text(self.session_json, json.dumps(meta, indent=2))

    def set_state(self, new_state: str) -> None:
        meta = self.read_meta()
        current = meta.get("state", "active")
        transition(current, new_state)
        if new_state != current:
            self.update_meta(state=new_state)

    def append_history(
        self,
        role: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        ticket_id: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": utcnow_iso(),
            "role": role,
            "content": content,
        }
        if tool_call_id is not None:
            entry["tool_call_id"] = tool_call_id
        if ticket_id is not None:
            entry["ticket_id"] = ticket_id
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with self.history_jsonl.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        current_count = self.read_meta().get("message_count", 0)
        self.update_meta(message_count=current_count + 1)

    def read_history(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return messages in OpenAI chat shape (role + content [+ tool_call_id])."""
        if not self.history_jsonl.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.history_jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if limit is not None:
            rows = rows[-limit:]
        out: list[dict[str, Any]] = []
        for r in rows:
            m: dict[str, Any] = {"role": r["role"], "content": r.get("content", "")}
            if r.get("tool_call_id"):
                m["tool_call_id"] = r["tool_call_id"]
            out.append(m)
        return out

    def append_trace(
        self,
        event: str,
        detail: dict[str, Any] | None = None,
        *,
        ticket_id: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"ts": utcnow_iso(), "event": event}
        if ticket_id is not None:
            entry["ticket_id"] = ticket_id
        if detail is not None:
            entry["detail"] = detail
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with self.trace_jsonl.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
