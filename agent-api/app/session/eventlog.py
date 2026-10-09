"""eventlog — what agent-api reads from a session's event log.

  load_runs(session_root) -> list[list[event]]   every run, in order
                                                 (protocol.replay.load_session)
  trace_rows(runs) -> list[row]                  the trace viewer's rows
  conversation(runs) -> list[{role, content}]    user text + settled answers

The event log is canonical; `history.jsonl` and `logs/trace.jsonl` are no
longer written (the projections kept for one release are retired). Trace
rows keep the shape the projection had: {ts, event, detail, duration_ms?},
with `ts` the event's own envelope ts. Event -> row:

  turn_start    turn_start          {turn_id, type, model}
  llm_response  assistant_response  {chars, turn_id, finish_reason}   (a text answer)
                llm_response        {turn_id, finish_reason, tool_calls: [names]}
  tool_call     tool_call           {name, arguments, call_id, side}
  op_state      op_state            {call_id, name, state} (+ duration_ms)
  tool_result   tool_result         {name, result (2000 chars), call_id} (+ duration_ms)
  question      question            {question_id, call_id, text (500 chars)}
  settle        settle              {input_id, outcome, usage}
  error         llm_error / loop_exhausted / error   {error (500 chars), class}
  memory        memory              {scope, op, slug, sha256, derived_from}
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from protocol.replay import load_session

USER_KINDS = ("task", "follow_up", "steer")

_ERROR_TRACE = {
    "transient": "llm_error", "overflow": "llm_error", "budget": "llm_error",
    "limit": "loop_exhausted", "protocol": "error", "fatal": "error",
}


def load_runs(session_root: Path | str) -> list[list[dict]]:
    """Raises protocol.replay.ReplayError on an unreadable log."""
    return load_session(Path(session_root))


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
            detail = {"name": call["name"], "result": content[:2000] if isinstance(content, str) else "",
                      "call_id": b.get("call_id")}
            if "duration_ms" in call:
                detail["duration_ms"] = call["duration_ms"]
            rows.append(_row(ts, "tool_result", detail, call.get("duration_ms")))
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


def trace_rows(runs: list[list[dict]], *, since: datetime | None = None,
               limit: int | None = None) -> list[dict]:
    """The trace rows of every run, oldest first; `since` keeps rows whose
    ts is at or after it (a row without a readable ts is kept only when
    there is no `since`), `limit` the last N."""
    rows = [r for events in runs for r in _run_rows(events)]
    if since is not None:
        rows = [r for r in rows if (t := parse_ts(r["ts"])) is not None and t >= since]
    if limit is not None:
        rows = rows[-limit:] if limit > 0 else []
    return rows


def conversation(runs: list[list[dict]], *, limit: int | None = None) -> list[dict]:
    """The conversation as the retired history.jsonl projection had it: the
    text of every new task/follow_up/steer input and every non-empty
    settled answer, in log order across runs."""
    out: list[dict] = []
    for events in runs:
        for ev in events:
            etype, b = ev.get("type"), ev.get("body") or {}
            if etype == "input" and b.get("dedupe") == "new" and b.get("kind") in USER_KINDS:
                out.append({"role": "user", "content": b.get("text", "")})
            elif etype == "settle" and b.get("answer"):
                out.append({"role": "assistant", "content": b["answer"]})
    if limit is not None:
        out = out[-limit:] if limit > 0 else []
    return out
