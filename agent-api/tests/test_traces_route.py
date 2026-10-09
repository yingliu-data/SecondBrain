"""Trace endpoints (M6-CONTRACTS §2): rows built from the session's event
log (every run), tenant scoping, since_minutes / limit; a migrated legacy
session is covered the same way."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import middleware
from app.routes import sessions as sessions_route
from app.session.dir_store import DirStore
from app.session.migrate import migrate_events
from app.tenants import TenantRegistry
from eventlog_fakes import LogBuilder, store

TWO_TENANT_DOC = {
    "tenants": [
        {"name": "wcc-event", "user": "wcc", "api_key": "key-event"},
        {"name": "wcc-analytic", "user": "wcc", "api_key": "key-analytic"},
    ]
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def signed_get(client, path, key):
    ts = str(int(time.time()))
    sig = hmac.new(key.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return client.get(path, headers={
        "Authorization": f"Bearer {key}", "X-Timestamp": ts, "X-Signature": sig})


@pytest.fixture
def dir_store(tmp_path):
    doc_path = tmp_path / "tenants.json"
    doc_path.write_text(json.dumps(TWO_TENANT_DOC))
    middleware.set_tenant_registry(TenantRegistry(doc_path, "env-key"))
    store_ = DirStore(tmp_path / "sessions")
    sessions_route.set_sessions(store_)
    return store_


@pytest.fixture
def client(dir_store):
    now = _iso(datetime.now(timezone.utc))
    session = dir_store.get_or_create("wcc", "wcc-event", "s1")
    store(session, LogBuilder(ts=now, user="wcc", tenant="wcc-event")
          .start().tool_task("in_1", "draft it", "drafted").events)
    app = FastAPI()
    app.include_router(sessions_route.router)
    return TestClient(app)


TOOL_TASK_ROWS = ["turn_start", "llm_response", "tool_call", "op_state", "tool_result",
                  "turn_start", "assistant_response", "settle"]


def test_session_traces_visible_to_owner(client):
    r = signed_get(client, "/api/v1/sessions/s1/traces", "key-event")
    assert r.status_code == 200
    rows = r.json()["traces"]
    assert [t["event"] for t in rows] == TOOL_TASK_ROWS
    call = next(t for t in rows if t["event"] == "tool_call")
    assert call["detail"] == {"name": "wcc-events__draft_event", "arguments": {"title": "x"},
                              "call_id": "c1", "side": "host"}
    result = next(t for t in rows if t["event"] == "tool_result")
    assert result["detail"]["result"] == "drafted" and result["duration_ms"] == 42
    assert result["detail"]["name"] == "wcc-events__draft_event"
    assert all(set(t) <= {"ts", "event", "detail", "duration_ms"} for t in rows)


def test_session_traces_limit_and_since(client):
    r = signed_get(client, "/api/v1/sessions/s1/traces?limit=2", "key-event")
    assert [t["event"] for t in r.json()["traces"]] == ["assistant_response", "settle"]
    r = signed_get(client, "/api/v1/sessions/s1/traces?since_minutes=5", "key-event")
    assert len(r.json()["traces"]) == len(TOOL_TASK_ROWS)


def test_traces_span_every_run(client, dir_store):
    session = dir_store.get("wcc", "wcc-event", "s1")
    store(session, LogBuilder(ts=_iso(datetime.now(timezone.utc)), user="wcc", tenant="wcc-event")
          .start().text_task("in_2", "again", "ok").events, new_run=True)
    r = signed_get(client, "/api/v1/sessions/s1/traces", "key-event")
    events = [t["event"] for t in r.json()["traces"]]
    assert events == TOOL_TASK_ROWS + ["turn_start", "assistant_response", "settle"]


def test_traces_recent_answers_last_minutes(client, dir_store):
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    stale = dir_store.get_or_create("wcc", "wcc-event", "s0")
    store(stale, LogBuilder(session_id="s0", ts=old, user="wcc", tenant="wcc-event")
          .start().text_task("in_0", "long ago", "yes").events)
    r = signed_get(client, "/api/v1/sessions/traces/recent?since_minutes=2", "key-event")
    assert r.status_code == 200
    traces = r.json()["traces"]
    assert [t["event"] for t in traces] == TOOL_TASK_ROWS
    assert all(t["session_id"] == "s1" for t in traces)
    r = signed_get(client, "/api/v1/sessions/traces/recent?since_minutes=600&limit=3",
                   "key-event")
    traces = r.json()["traces"]
    assert len(traces) == 3 and traces[-1]["session_id"] == "s1"
    r = signed_get(client, "/api/v1/sessions/traces/recent?since_minutes=600", "key-event")
    assert [t["session_id"] for t in r.json()["traces"]][:3] == ["s0", "s0", "s0"]


def test_sibling_tenant_cannot_see_traces(client):
    """Same user, different tenant token → that tenant sees nothing."""
    r = signed_get(client, "/api/v1/sessions/s1/traces", "key-analytic")
    assert r.status_code == 404
    r = signed_get(client, "/api/v1/sessions/traces/recent", "key-analytic")
    assert r.status_code == 200 and r.json()["traces"] == []
    r = signed_get(client, "/api/v1/sessions", "key-analytic")
    assert r.json()["sessions"] == []


def test_list_sessions_scoped(client):
    r = signed_get(client, "/api/v1/sessions", "key-event")
    assert [s["session_id"] for s in r.json()["sessions"]] == ["s1"]


def test_migrated_session_traces(client, dir_store):
    """A legacy session converted by migrate --events: its traces come from
    the migrated run like any harness session's."""
    legacy = dir_store.get_or_create("wcc", "wcc-event", "old")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = [{"ts": _iso(t0), "role": "user", "content": "draft an event", "ticket_id": "tk_a"},
            {"ts": _iso(t0 + timedelta(seconds=5)), "role": "assistant",
             "content": "Drafted.", "ticket_id": "tk_a"}]
    legacy.history_jsonl.write_text("".join(json.dumps(r) + "\n" for r in rows))
    legacy.trace_jsonl.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": _iso(t0 + timedelta(seconds=1)), "event": "tool_call", "ticket_id": "tk_a",
         "detail": {"name": "wcc-events__draft_event", "arguments": {"title": "x"}}},
        {"ts": _iso(t0 + timedelta(seconds=2)), "event": "tool_result", "ticket_id": "tk_a",
         "detail": {"name": "wcc-events__draft_event", "result": "ok", "duration_ms": 7}}]))
    assert migrate_events(dir_store.root, quiet=True)["stats"] == {"migrated": 1}
    r = signed_get(client, "/api/v1/sessions/old/traces", "key-event")
    traces = r.json()["traces"]
    assert [t["event"] for t in traces] == TOOL_TASK_ROWS
    result = next(t for t in traces if t["event"] == "tool_result")
    assert result["duration_ms"] == 7 and result["detail"]["result"] == "ok"
    # ts are the legacy rows' timestamps
    call = next(t for t in traces if t["event"] == "tool_call")
    assert call["ts"] == _iso(t0 + timedelta(seconds=1))
    assert traces[-1]["ts"] == rows[1]["ts"]
    r = signed_get(client, "/api/v1/sessions/traces/recent?since_minutes=5", "key-event")
    assert {t["session_id"] for t in r.json()["traces"]} == {"s1", "old"}
