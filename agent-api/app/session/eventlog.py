"""eventlog — what agent-api reads from a session's event log.

  read_trace(session_root, since=, limit=) -> list[row]   the trace viewer's rows
  trace_rows(runs) -> list[row]                           the same from runs in memory
  session_turns(session_root) -> list[Turn]               the conversation, for the memory index
  memory_changes(session_root) -> {slug: op}              the user memory files it wrote or removed

The event log is canonical; `history.jsonl` and `logs/trace.jsonl` are no
longer written (the projections kept for one release are retired). Trace
rows keep the shape the projection had: {ts, event, detail, duration_ms?},
with `ts` the event's own envelope ts. Event -> row:

  turn_start    turn_start          {turn_id, type, model}
  llm_response  assistant_response  {chars, turn_id, finish_reason}   (a text answer)
                llm_response        {turn_id, finish_reason, tool_calls: [names]}
  tool_call     tool_call           {name, arguments, call_id, side}
  op_state      op_state            {call_id, name, state}   (+ row duration_ms)
  tool_result   tool_result         {name, result (2000 chars), call_id}
                                                     (+ row duration_ms: the call's op_state's)
  question      question            {question_id, call_id, text (500 chars)}
  settle        settle              {input_id, outcome, usage}
  error         llm_error / loop_exhausted / error   {error (500 chars), class}
  memory        memory              {scope, op, slug, sha256, derived_from}
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ast_everything.memory import Turn
from protocol.replay import iter_runs

from app.user.memory import _VALID_SLUG

_ERROR_TRACE = {
    "transient": "llm_error", "overflow": "llm_error", "budget": "llm_error",
    "limit": "loop_exhausted", "protocol": "error", "fatal": "error",
}


def parse_ts(ts) -> datetime | None:
    """An envelope ts as an aware datetime (None when it is not ISO-8601)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row(ts, event: str, detail: dict, duration_ms=None) -> dict:
    row = {"ts": ts, "event": event, "detail": detail}
    if duration_ms is not None:
        row["duration_ms"] = duration_ms
    return row


def _run_rows(events: list[dict]) -> list[dict]:
    rows: list[dict] = []
    calls: dict[str, dict] = {}            # call_id -> {name, duration_ms?}
    for ev in events:
        etype, b, ts = ev.get("type"), ev.get("body") or {}, ev.get("ts")
        if etype == "turn_start":
            rows.append(_row(ts, "turn_start", {"turn_id": b.get("turn_id"), "type": b.get("type"),
                                                "model": b.get("model")}))
        elif etype == "llm_response":
            msg = b.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            if tool_calls or b.get("finish_reason") == "tool_calls":
                names = [(c.get("function") or {}).get("name") for c in tool_calls
                         if isinstance(c, dict)]
                rows.append(_row(ts, "llm_response", {"turn_id": b.get("turn_id"),
                                                      "finish_reason": b.get("finish_reason"),
                                                      "tool_calls": names}))
            else:
                text = msg.get("content")
                text = text.strip() if isinstance(text, str) else ""
                rows.append(_row(ts, "assistant_response", {
                    "chars": len(text), "turn_id": b.get("turn_id"),
                    "finish_reason": b.get("finish_reason")}))
        elif etype == "tool_call":
            calls[b.get("call_id")] = {"name": b.get("name")}
            rows.append(_row(ts, "tool_call", {"name": b.get("name"), "arguments": b.get("args"),
                                               "call_id": b.get("call_id"), "side": b.get("side")}))
        elif etype == "op_state":
            call = calls.setdefault(b.get("call_id"), {"name": "?"})
            if "duration_ms" in b:
                call["duration_ms"] = b["duration_ms"]
            rows.append(_row(ts, "op_state", {"call_id": b.get("call_id"), "name": call["name"],
                                              "state": b.get("state")}, b.get("duration_ms")))
        elif etype == "tool_result":
            call = calls.get(b.get("call_id"), {"name": "?"})
            content = b.get("content")
            rows.append(_row(ts, "tool_result", {
                "name": call["name"], "result": content[:2000] if isinstance(content, str) else "",
                "call_id": b.get("call_id")}, call.get("duration_ms")))
        elif etype == "question":
            rows.append(_row(ts, "question", {"question_id": b.get("question_id"),
                                              "call_id": b.get("call_id"),
                                              "text": str(b.get("text", ""))[:500]}))
        elif etype == "settle":
            rows.append(_row(ts, "settle", {"input_id": b.get("input_id"),
                                            "outcome": b.get("outcome"), "usage": b.get("usage")}))
        elif etype == "error":
            rows.append(_row(ts, _ERROR_TRACE.get(b.get("class"), "error"),
                             {"error": str(b.get("message", ""))[:500], "class": b.get("class")}))
        elif etype == "memory":
            rows.append(_row(ts, "memory", {k: b.get(k) for k in
                                            ("scope", "op", "slug", "sha256", "derived_from")}))
    return rows


def _since(rows: list[dict], since: datetime | None) -> list[dict]:
    """The rows whose ts is at or after `since` (all of them when None; a
    row without a readable ts only then)."""
    if since is None:
        return rows
    return [r for r in rows if (t := parse_ts(r["ts"])) is not None and t >= since]


def _last(rows: list[dict], limit: int | None) -> list[dict]:
    if limit is None:
        return rows
    return rows[-limit:] if limit > 0 else []


def trace_rows(runs: list[list[dict]], *, since: datetime | None = None,
               limit: int | None = None) -> list[dict]:
    """The trace rows of every run, oldest first; `since` keeps rows whose
    ts is at or after it (a row without a readable ts is kept only when
    there is no `since`), `limit` the last N."""
    return _last(_since([r for events in runs for r in _run_rows(events)], since), limit)


def read_trace(session_root: Path | str, *, since: datetime | None = None,
               limit: int | None = None) -> list[dict]:
    """trace_rows of the session's log, read newest run first and stopping
    once `limit` rows are in hand or a run reaches back before `since`; a
    run last written before `since` is not read at all (its segments'
    mtime). The host log is not read. Raises protocol.replay.ReplayError
    on an unreadable log."""
    out: list[dict] = []
    runs = iter_runs(Path(session_root), newest_first=True,
                     modified_since=since.timestamp() if since is not None else None)
    for events in runs:
        rows = _run_rows(events)
        kept = _since(rows, since)
        out[:0] = kept
        if len(kept) < len(rows) or (limit is not None and len(out) >= limit):
            break
    return _last(out, limit)


def session_turns(session_root: Path | str) -> list[Turn]:
    """The conversation of every run, oldest first, numbered from 1: the text of each new
    input (`user`) and each text answer with no tool calls (`assistant`), at its event's
    ts. The summary of a compaction turn is not a turn."""
    turns: list[Turn] = []
    for events in iter_runs(Path(session_root)):
        compactions = {ev["body"].get("turn_id") for ev in events
                       if ev.get("type") == "turn_start" and ev["body"].get("type") == "compaction"}
        for ev in events:
            if said := _said(ev, compactions):
                turns.append(Turn(len(turns) + 1, said[0], parse_ts(ev.get("ts")), said[1]))
    return turns


def _said(ev: dict, compactions: set) -> tuple[str, str] | None:
    """(said_by, text) when the event is a turn of the conversation."""
    b = ev.get("body") or {}
    if ev.get("type") == "input" and b.get("dedupe") == "new" and b.get("text"):
        return "user", b["text"]
    msg = b.get("message") or {}
    text = msg.get("content")
    if (ev.get("type") == "llm_response" and not msg.get("tool_calls") and b.get("turn_id") not in compactions
            and isinstance(text, str) and text.strip()):
        return "assistant", text
    return None


def memory_changes(session_root: Path | str) -> dict[str, str]:
    """{slug: its last op} of the session's `memory` events of the user scope. A slug the memory
    store would refuse is left out: the guest writes the log, and a slug becomes a path."""
    changes: dict[str, str] = {}
    for events in iter_runs(Path(session_root)):
        for ev in events:
            b = ev.get("body") or {}
            if ev.get("type") == "memory" and b.get("scope") == "user" and _VALID_SLUG.fullmatch(b["slug"]):
                changes[b["slug"]] = b["op"]
    return changes
