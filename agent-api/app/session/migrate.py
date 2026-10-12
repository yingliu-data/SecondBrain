"""Session migrations.

0. session directory layout {tenant}_{session_id} → {tenant}.{session_id}:

     python -m app.session.migrate --layout [--dry-run] [<sessions_root>]

   The old name is ambiguous (tenant `wcc` + session `event_x` and tenant
   `wcc_event` + session `x` share `wcc_event_x`); agent-api still reads a
   legacy directory in place, but only for the session its session.json
   names. This renames every legacy directory whose name is exactly
   `{tenant}_{session_id}` of its own session.json to the injective
   `{tenant}.{session_id}` (app.session.dir_store). Skipped: a directory
   whose session.json is missing/invalid or names another session
   (`mismatch`), and one whose new name is taken (`target_exists`). Run it
   with agent-api stopped (a parked VM's snapshot is relocatable: its
   Firecracker paths are relative to the session directory). Idempotent.

1. conversations.db rows → session directories (one-time, legacy store):

     python -m app.session.migrate [--db data/conversations.db] [--root data/sessions]

   "abc"           (un-namespaced) → user "default", tenant "default"
   "wcc-event:abc" (namespaced)    → tenant "wcc-event" under its configured user
                                     (resolved via tenants.json; fallback: user = tenant)
   Each conversation's user/assistant messages become the event log run
   `events/r0000` exactly as step 2 builds one (line L = the Lth of those
   messages, ts = the migration time). Idempotent: existing session dirs
   are skipped. SQLite is left untouched.

2. legacy `history.jsonl` → event log run `events/r0000`:

     python -m app.session.migrate --events [--dry-run] <sessions_root>

   Every session directory ({root}/{user}/{tenant}.{session_id}/, or a legacy
   {tenant}_{session_id}/) with a
   non-empty `history.jsonl` and no run under `events/` gets one run:

     session_start  user/tenant from session.json, resumed: false,
                    migrated: true, tools = the legacy tools the history used
                    (side host; [] when none)
     per user message (line L):  input{task, input_id = derive_key_id("input",
                    user, tenant, session_id, "L")}
     per legacy tool call:       clock{turn_clock} + turn_start + llm_response
                    (the assistant tool_calls message) + tool_call{side: host}
                    + op_state{done, duration_ms} when the duration is known
                    + tool_result
     per assistant reply:        clock{turn_clock} + turn_start{regular} +
                    llm_response{message: {role: assistant, content}}
     settle         {outcome: done, answer: the last reply} per user message
                    (failed with an empty answer when it never got a reply)
     checkpoint     through the last event, chained to session_start

   Tool calls come from the history (`assistant` rows with `tool_calls`,
   `tool` rows with `tool_call_id`) when present, else from the legacy
   `logs/trace.jsonl` tool_call / tool_result rows of the same ticket_id.
   `clock.at` and the envelope `ts` come from the rows' timestamps (data
   only). Assistant rows before the first user message are skipped (counted).
   Every run is checked with protocol.fold.replay (validate + fold +
   checkpoint chain) before it is written, and written through a temporary
   directory renamed to `events/r0000` (state_sync.replace_run_dir), so a
   session either has its whole run or none. Idempotent: a session whose
   `events/` holds a run is skipped. The legacy files are left in place
   (nothing reads them any more).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from protocol.canonical import derive_id, derive_key_id, hash_value
from protocol.clock import now_iso
from protocol.events import make_event
from protocol.fold import checkpoint_body, replay
from protocol.limits import SEGMENT_EVENTS
from protocol.replay import run_name, run_numbers

from app.config import SESSION_DB_PATH, SESSIONS_ROOT
from app.host.state_sync import replace_run_dir
from app.session.dir_store import DirStore
from app.session.eventlog import parse_ts
from app.session.ids import is_safe_id
from app.session.session_dir import SessionDir

LEGACY_MODEL = "legacy"
LEGACY_TOOL = "legacy_tool"          # a legacy tool row that names no tool
NO_RESULT = "[no result recorded]"
HISTORY_FILE = "history.jsonl"       # the legacy files, relative to the session dir
TRACE_FILE = "logs/trace.jsonl"
MIGRATED_RUN = 0                     # the run a migration writes: events/r0000
_EPOCH_TS = "1970-01-01T00:00:00.000Z"


# ---------------------------------------------------------------- sqlite → dirs
def _tenant_users() -> dict[str, str]:
    try:
        from app.tenants import create_tenant_registry
        return {t.name: t.user for t in create_tenant_registry().all_tenants()}
    except Exception:
        return {}


def migrate(db_path: str = SESSION_DB_PATH, root: str = SESSIONS_ROOT,
            segment_events: int = SEGMENT_EVENTS) -> dict:
    store = DirStore(root)
    users = _tenant_users()
    stats = {"migrated": 0, "skipped_existing": 0, "skipped_invalid": 0, "failed": 0}

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT session_id, messages, updated_at FROM sessions").fetchall()
    conn.close()

    for store_key, messages_json, updated_at in rows:
        if ":" in store_key:
            tenant, _, session_id = store_key.partition(":")
        else:
            tenant, session_id = "default", store_key
        user = users.get(tenant, tenant)

        if not (is_safe_id(user) and is_safe_id(tenant) and is_safe_id(session_id)):
            print(f"  SKIP (unsafe id): {store_key!r}")
            stats["skipped_invalid"] += 1
            continue
        if store.get(user, tenant, session_id) is not None:
            stats["skipped_existing"] += 1
            continue

        session = store.get_or_create(user, tenant, session_id)
        try:
            messages = json.loads(messages_json)
        except json.JSONDecodeError:
            messages = []
        now = now_iso()
        history = list(enumerate(
            ({"ts": now, "role": m["role"], "content": m.get("content") or ""}
             for m in messages if m.get("role") in ("user", "assistant")), start=1))
        session.update_meta(title=f"migrated from sqlite ({updated_at})")
        if history:
            meta = {"user": user, "tenant": tenant, "session_id": session_id,
                    "created_at": session.read_meta().get("created_at")}
            events, _counts, problems = build_events(meta, history)
            if problems:
                print(f"  FAILED {store_key!r}: {problems[:3]}")
                stats["failed"] += 1
                continue
            write_run(session, events, segment_events)
        print(f"  {store_key!r} -> {user}/{tenant}.{session_id} ({len(messages)} msgs)")
        stats["migrated"] += 1

    print(f"Done: {stats}")
    return stats


# ---------------------------------------------------------------- legacy files
def _read_jsonl(path: Path) -> list[tuple[int, dict]]:
    """(1-based line number, row) of every parseable dict line."""
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append((n, row))
    return out


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {"raw": raw}
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    return {}


def _text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _trace_tools(session: SessionDir) -> dict[str, list[dict]]:
    """ticket_id -> the legacy tool calls of that turn, from logs/trace.jsonl:
    each tool_call paired with the next tool_result of the same name."""
    by_ticket: dict[str, list[dict]] = {}
    open_calls: dict[str, list[dict]] = {}
    for _, row in _read_jsonl(session.root / TRACE_FILE):
        ticket, event = row.get("ticket_id"), row.get("event")
        detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        if not isinstance(ticket, str) or event not in ("tool_call", "tool_result"):
            continue
        name = detail.get("name")
        if event == "tool_call":
            if not isinstance(name, str) or not name:
                continue
            call = {"call_id": None, "name": name, "args": _args(detail.get("arguments")),
                    "result": None, "ts": row.get("ts"), "result_ts": None, "duration_ms": None}
            by_ticket.setdefault(ticket, []).append(call)
            open_calls.setdefault(ticket, []).append(call)
            continue
        pending = open_calls.get(ticket, [])
        call = next((c for c in pending if c["name"] == name), pending[0] if pending else None)
        if call is None:
            continue
        pending.remove(call)
        call["result"] = _text(detail.get("result"))
        call["result_ts"] = row.get("ts")
        d = detail.get("duration_ms", row.get("duration_ms"))
        if isinstance(d, int) and not isinstance(d, bool) and d >= 0:
            call["duration_ms"] = d
    return by_ticket


def _exchanges(rows: list[tuple[int, dict]]) -> tuple[list[dict], int]:
    """Group the history into exchanges: a user row and the steps after it
    (tool calls and assistant replies) up to the next user row. Returns
    (exchanges, rows skipped)."""
    exchanges: list[dict] = []
    current = None
    announced: dict[str, tuple[str, dict]] = {}     # tool_call id -> (name, args)
    skipped = 0
    for line_no, row in rows:
        role = row.get("role")
        if role == "user":
            current = {"line": line_no, "row": row, "steps": [], "history_tools": False}
            exchanges.append(current)
            continue
        if current is None or role not in ("assistant", "tool"):
            skipped += 1
            continue
        if role == "assistant" and row.get("tool_calls"):
            for c in row["tool_calls"] if isinstance(row["tool_calls"], list) else []:
                fn = c.get("function") if isinstance(c, dict) else None
                if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(fn, dict):
                    announced[c["id"]] = (_text(fn.get("name")) or LEGACY_TOOL,
                                          _args(fn.get("arguments")))
            if not _text(row.get("content")).strip():
                continue
        if role == "tool":
            cid = row.get("tool_call_id") if isinstance(row.get("tool_call_id"), str) else None
            name, args = announced.get(cid, (_text(row.get("name")) or LEGACY_TOOL, {}))
            current["steps"].append(("tool", {
                "call_id": cid, "name": name, "args": args, "result": _text(row.get("content")),
                "ts": row.get("ts"), "result_ts": row.get("ts"), "duration_ms": None}))
            current["history_tools"] = True
            continue
        current["steps"].append(("reply", row))
    return exchanges, skipped


# ---------------------------------------------------------------- events
class _Log:
    """Builds one run: gapless seqs, ts / clock from the legacy rows."""

    def __init__(self, session_id: str, user: str, tenant: str, first_ts: str):
        self.session_id, self.user, self.tenant = session_id, user, tenant
        self.events: list[dict] = []
        self.ts = first_ts
        self.at = 0

    @property
    def next_seq(self) -> int:
        return len(self.events) + 1

    def at_ts(self, ts) -> None:
        if parse_ts(ts) is not None:
            self.ts = ts
            self.at = int(parse_ts(ts).timestamp() * 1000)

    def emit(self, type: str, body: dict) -> dict:
        ev = make_event(self.session_id, self.next_seq, self.ts, type, body)
        self.events.append(ev)
        return ev

    def derived(self, kind: str) -> str:
        return derive_id(self.user, self.tenant, self.session_id, self.next_seq, kind)

    def turn(self, message: dict, finish_reason: str) -> str:
        """clock{turn_clock} + turn_start + llm_response; returns the turn_id."""
        self.emit("clock", {"kind": "turn_clock", "at": self.at})
        turn_id = self.derived("turn")
        self.emit("turn_start", {
            "turn_id": turn_id, "type": "regular", "includes_through_seq": self.next_seq - 1,
            "model": LEGACY_MODEL, "params": {},
            "request_hash": hash_value(["migrated", self.session_id, self.next_seq]),
            "preamble_hash": hash_value(""), "workspace_tree_id": None})
        self.emit("llm_response", {"turn_id": turn_id, "message": message,
                                   "finish_reason": finish_reason, "usage": {}})
        return turn_id


def build_events(meta: dict, history: list[tuple[int, dict]],
                 trace_tools: dict[str, list[dict]] | None = None
                 ) -> tuple[list[dict], dict, list[str]]:
    """The run r0000 of a legacy session: (events, counts, problems), the
    problems of protocol.fold.replay over it ([] == it verifies)."""
    user, tenant, session_id = meta["user"], meta["tenant"], meta["session_id"]
    trace_tools = trace_tools or {}
    exchanges, skipped = _exchanges(history)
    for ex in exchanges:
        ticket = ex["row"].get("ticket_id")
        if not ex["history_tools"] and isinstance(ticket, str) and trace_tools.get(ticket):
            first_reply = next((i for i, (k, _) in enumerate(ex["steps"]) if k == "reply"),
                               len(ex["steps"]))
            ex["steps"][first_reply:first_reply] = [("tool", c) for c in trace_tools[ticket]]
    tool_names = sorted({s["name"] for ex in exchanges for k, s in ex["steps"] if k == "tool"})

    first_ts = next((r.get("ts") for _, r in history if parse_ts(r.get("ts"))), None)
    log = _Log(session_id, user, tenant, first_ts or meta.get("created_at") or _EPOCH_TS)
    log.at_ts(log.ts)
    log.emit("session_start", {
        "user": user, "tenant": tenant, "workflow_run_id": f"{user}/{tenant}/{session_id}",
        "config_hash": hash_value({"migrated": "history.jsonl"}),
        "system_hash": hash_value({"migrated": "history.jsonl"}),
        "tools": [{"name": n, "side": "host", "pin": None} for n in tool_names],
        "harness_version": LEGACY_MODEL, "image_version": LEGACY_MODEL,
        "resumed": False, "migrated": True})

    counts = {"exchanges": len(exchanges), "replies": 0, "tool_calls": 0,
              "unanswered": 0, "skipped_rows": skipped}
    used_ids: set[str] = set()
    for ex in exchanges:
        row = ex["row"]
        log.at_ts(row.get("ts"))
        input_id = derive_key_id("input", user, tenant, session_id, str(ex["line"]))
        log.emit("input", {"input_id": input_id, "kind": "task",
                           "text": _text(row.get("content")), "dedupe": "new"})
        answer = None
        for kind, step in ex["steps"]:
            if kind == "reply":
                log.at_ts(step.get("ts"))
                answer = _text(step.get("content"))
                log.turn({"role": "assistant", "content": answer}, "stop")
                counts["replies"] += 1
                continue
            log.at_ts(step.get("ts"))
            call_id = step["call_id"]
            if not call_id or call_id in used_ids:
                call_id = derive_id(user, tenant, session_id, log.next_seq + 3, "call")
            used_ids.add(call_id)
            turn_id = log.turn({"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": step["name"],
                             "arguments": json.dumps(step["args"], ensure_ascii=False)}}]},
                "tool_calls")
            log.emit("tool_call", {"turn_id": turn_id, "call_id": call_id, "name": step["name"],
                                   "side": "host", "args": step["args"], "state": "RUNNING"})
            log.at_ts(step.get("result_ts"))
            if step.get("duration_ms") is not None:
                log.emit("op_state", {"call_id": call_id, "state": "done",
                                      "duration_ms": step["duration_ms"]})
            result = step["result"]
            log.emit("tool_result", {"call_id": call_id,
                                     "content": NO_RESULT if result is None else result})
            counts["tool_calls"] += 1
        if answer is None:
            counts["unanswered"] += 1
        log.emit("settle", {"input_id": input_id, "outcome": "done" if answer is not None else "failed",
                            "answer": answer or "", "usage": {}, "manifest": {}})

    state, problems = replay(log.events)     # one fold: verifies, and seals the run
    log.emit("checkpoint", checkpoint_body(log.events, state=state))
    counts["events"] = len(log.events)
    return log.events, counts, problems


def _meta(session: SessionDir) -> dict | None:
    """user / tenant / session_id of a session directory, from session.json
    (tenant falls back to the directory name {tenant}.{session_id} or the
    legacy {tenant}_{session_id})."""
    try:
        meta = session.read_meta()
    except (OSError, json.JSONDecodeError):
        return None
    session_id = meta.get("session_id")
    user = meta.get("user_id") or session.root.parent.name
    tenant = meta.get("tenant") or ""
    name = session.root.name
    if not tenant and isinstance(session_id, str):
        for sep in (".", "_"):              # {tenant}.{sid}, legacy {tenant}_{sid}
            if name.endswith(sep + session_id):
                tenant = name[: -len(session_id) - 1]
                break
    if not all(isinstance(v, str) and is_safe_id(v) for v in (user, tenant, session_id)):
        return None
    return {"user": user, "tenant": tenant, "session_id": session_id,
            "created_at": meta.get("created_at")}


def write_run(session: SessionDir, events: list[dict],
              segment_events: int = SEGMENT_EVENTS) -> Path:
    """Write `events` as events/r0000 (segments as StateSync lays them out),
    through a temporary directory renamed into place."""
    target = session.events / run_name(MIGRATED_RUN)
    session.events.mkdir(exist_ok=True)
    replace_run_dir(target, events, segment_events)
    return target


def migrate_session(session: SessionDir, *, dry_run: bool = False,
                    segment_events: int = SEGMENT_EVENTS) -> dict:
    """Convert one session; returns its report {status, ...counts}."""
    if run_numbers(session.events):
        return {"status": "skipped_existing"}
    history = _read_jsonl(session.root / HISTORY_FILE)
    if not history:
        return {"status": "skipped_empty"}
    meta = _meta(session)
    if meta is None:
        return {"status": "skipped_invalid"}
    events, counts, problems = build_events(meta, history, _trace_tools(session))
    if problems:
        return {"status": "failed", "problems": problems[:5], **counts}
    if dry_run:
        return {"status": "would_migrate", **counts}
    write_run(session, events, segment_events)
    return {"status": "migrated", **counts}


def _session_dirs(root: Path):
    for user_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        for entry in sorted(p for p in user_dir.iterdir() if p.is_dir()):
            if (entry / "session.json").exists() and (entry / HISTORY_FILE).exists():
                yield SessionDir(entry)


def migrate_events(root: str | Path = SESSIONS_ROOT, *, dry_run: bool = False,
                   segment_events: int = SEGMENT_EVENTS, quiet: bool = False) -> dict:
    """Every legacy session under `root` → events/r0000. Returns
    {stats: {status: count}, sessions: {relative path: report}}."""
    root = Path(root)
    stats: dict[str, int] = {}
    sessions: dict[str, dict] = {}
    for session in _session_dirs(root):
        try:
            report = migrate_session(session, dry_run=dry_run, segment_events=segment_events)
        except Exception as e:     # one bad session never stops the rest
            report = {"status": "failed", "problems": [f"{type(e).__name__}: {e}"]}
        rel = str(session.root.relative_to(root))
        sessions[rel] = report
        stats[report["status"]] = stats.get(report["status"], 0) + 1
        if not quiet and report["status"] not in ("skipped_existing", "skipped_empty"):
            extra = {k: v for k, v in report.items() if k != "status"}
            print(f"  {rel}: {report['status']} {json.dumps(extra, ensure_ascii=False)}")
    if not quiet:
        print(f"Done{' (dry run)' if dry_run else ''}: {stats}")
    return {"stats": stats, "sessions": sessions}


# ---------------------------------------------------------------- layout
def migrate_layout(root: str | Path = SESSIONS_ROOT, *, dry_run: bool = False,
                   quiet: bool = False) -> dict:
    """Rename legacy `{tenant}_{session_id}` session directories to
    `{tenant}.{session_id}`. Returns {stats: {status: count}, sessions:
    {old relative path: status}}; status renamed | would_rename |
    target_exists | mismatch."""
    from app.session.dir_store import read_identity
    from app.session.ids import legacy_dir_name, session_dir_name
    root = Path(root)
    stats: dict[str, int] = {}
    sessions: dict[str, str] = {}
    user_dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for user_dir in user_dirs:
        for entry in sorted(p for p in user_dir.iterdir() if p.is_dir()):
            ident = read_identity(entry)
            if ident is None:
                continue
            tenant, sid, user = ident["tenant"], ident["session_id"], ident["user"]
            if not all(isinstance(v, str) and is_safe_id(v) for v in (tenant, sid)):
                status = "mismatch"
            elif entry.name == session_dir_name(tenant, sid):
                continue                                    # already the new layout
            elif entry.name != legacy_dir_name(tenant, sid) or user not in (None, user_dir.name):
                status = "mismatch"
            else:
                target = user_dir / session_dir_name(tenant, sid)
                if target.exists():
                    status = "target_exists"
                elif dry_run:
                    status = "would_rename"
                else:
                    entry.rename(target)
                    status = "renamed"
            rel = str(entry.relative_to(root))
            sessions[rel] = status
            stats[status] = stats.get(status, 0) + 1
            if not quiet:
                print(f"  {rel}: {status}")
    if not quiet:
        print(f"Done{' (dry run)' if dry_run else ''}: {stats}")
    return {"stats": stats, "sessions": sessions}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--events", action="store_true",
                        help="convert legacy history.jsonl sessions to event logs")
    parser.add_argument("--layout", action="store_true",
                        help="rename legacy {tenant}_{session_id} session directories "
                             "to {tenant}.{session_id}")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --events / --layout: report what would change, change nothing")
    parser.add_argument("sessions_root", nargs="?", default=None)
    parser.add_argument("--db", default=SESSION_DB_PATH)
    parser.add_argument("--root", default=SESSIONS_ROOT)
    args = parser.parse_args(argv)
    if args.layout:
        migrate_layout(args.sessions_root or args.root, dry_run=args.dry_run)
        return 0
    if args.events:
        result = migrate_events(args.sessions_root or args.root, dry_run=args.dry_run)
        return 1 if result["stats"].get("failed") else 0
    if args.dry_run:
        parser.error("--dry-run needs --events or --layout")
    migrate(args.db, args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
