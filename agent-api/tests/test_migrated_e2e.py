"""A legacy session migrated to an event log continues under the harness
engine (M6-CONTRACTS §4): the worker's first boot sends hello{resume: true},
the real guest (LocalSandbox subprocess) takes the migrated run as its log
return and folds it, so the next answer's LLM request carries the migrated
conversation; the run continues r0000's seqs and still verifies.

The worker side (Worker._boot): a fresh boot of a session whose current run
is migrated and not yet continued (`StateSync.migrated_run`) is a resume,
and the run's allowlist is re-pinned to the hello's tools first
(`StateSync.retool_migrated`).
"""
import json

import pytest

from protocol.fold import replay
from protocol.replay import verify_session

from app.session.migrate import migrate_session
from test_host_e2e import make_worker, tool_call, upstream  # noqa: F401
from host_fakes import session, users_root  # noqa: F401

HISTORY = [
    {"ts": "2026-05-01T10:15:02.117Z", "role": "user", "content": "My dog is called Biscuit.",
     "ticket_id": "tk_1"},
    {"ts": "2026-05-01T10:15:05.000Z", "role": "assistant", "content": "Nice name! Noted.",
     "ticket_id": "tk_1"},
    {"ts": "2026-05-01T10:16:00.000Z", "role": "user", "content": "Find a vet near me",
     "ticket_id": "tk_2"},
    {"ts": "2026-05-01T10:16:09.000Z", "role": "assistant", "content": "Try Paws Clinic.",
     "ticket_id": "tk_2"},
]
TRACE = [
    {"ts": "2026-05-01T10:16:03.000Z", "event": "tool_call", "ticket_id": "tk_2",
     "detail": {"name": "web_search", "arguments": {"q": "vet near me"}}},
    {"ts": "2026-05-01T10:16:05.000Z", "event": "tool_result", "ticket_id": "tk_2",
     "detail": {"name": "web_search", "result": "Paws Clinic, 2km", "duration_ms": 2000}},
]


def _migrate(session):
    session.history_jsonl.write_text("".join(json.dumps(r) + "\n" for r in HISTORY))
    session.trace_jsonl.write_text("".join(json.dumps(r) + "\n" for r in TRACE))
    report = migrate_session(session)
    assert report["status"] == "migrated", report
    return report


@pytest.mark.asyncio
async def test_migrated_session_continues_with_its_history(session, upstream):  # noqa: F811
    report = _migrate(session)
    upstream.responses = [{"content": "Biscuit, as you told me."}]
    w = make_worker(session, upstream)
    assert w.sync.migrated_run and w.sync.run == 0
    try:
        result = await w.act({"input_id": "in_next", "text": "What is my dog called?"})
        assert result["outcome"] == "done", result
        assert result["answer"] == "Biscuit, as you told me."
        assert w.last_hello["resume"] is True

        # the request the model saw: system, the migrated conversation (tool
        # round included), then the new question
        msgs = upstream.requests[0]["messages"]
        assert msgs[0]["role"] == "system"
        convo = [(m["role"], m.get("content")) for m in msgs[1:]]
        assert convo[0] == ("user", "My dog is called Biscuit.")
        assert convo[1] == ("assistant", "Nice name! Noted.")
        assert convo[2] == ("user", "Find a vet near me")
        assert msgs[4]["tool_calls"][0]["function"]["name"] == "web_search"
        assert convo[4] == ("tool", "Paws Clinic, 2km")
        assert convo[5] == ("assistant", "Try Paws Clinic.")
        assert convo[-1][0] == "user" and convo[-1][1].endswith("What is my dog called?")
        assert len(convo) == 7

        # the run continued r0000's seqs, no new run; it still verifies
        assert w.sync.run == 0
        assert sorted(p.name for p in (session.root / "events").glob("r*")) == ["r0000"]
        events = w.sync.read(0)
        assert events[report["events"]]["seq"] == report["events"] + 1
        assert replay(events)[1] == []
        assert verify_session(session.root) == []
        assert session.read_history()[-2:] == [
            {"role": "user", "content": "What is my dog called?"},
            {"role": "assistant", "content": "Biscuit, as you told me."}]
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_migrated_session_can_call_the_tenants_tools(session, upstream):  # noqa: F811
    """The migrated run's own allowlist holds only the legacy tools; the
    first boot re-pins it to the hello's tools, so a guest built-in and a
    host tool both run after the resume (without it the guest dies on the
    first call the migrated session_start does not offer)."""
    _migrate(session)
    upstream.responses = [tool_call("call_b", "bash", {"command": "echo hi"}),
                          tool_call("call_w", "web_search", {"q": "vets"}),
                          {"content": "done"}]
    w = make_worker(session, upstream)
    try:
        result = await w.act({"input_id": "in_t", "text": "run"})
        assert result["outcome"] == "done", result
        events = w.sync.read(0)
        start = events[0]["body"]
        assert start["migrated"] is True
        names = {t["name"] for t in start["tools"]}
        assert {"bash", "web_search", "get_calendar_events"} <= names
        calls = [e["body"]["name"] for e in events if e["type"] == "tool_call"]
        assert calls == ["web_search", "bash", "web_search"]      # legacy, then the new two
        assert verify_session(session.root) == []
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_only_the_first_boot_resumes_a_migrated_run(session, upstream):  # noqa: F811
    """Once a guest continued the migrated run it is an ordinary run: a later
    fresh boot (a new worker, no parked snapshot) opens a new run as usual."""
    _migrate(session)
    upstream.responses = [{"content": "ok"}]
    w = make_worker(session, upstream)
    try:
        assert (await w.act({"input_id": "in_x", "text": "hi"}))["outcome"] == "done"
        assert w.last_hello["resume"] is True and w.sync.run == 0
    finally:
        await w.close()
    w2 = make_worker(session, upstream)
    assert w2.sync.migrated_run is False
    try:
        assert (await w2.act({"input_id": "in_y", "text": "again"}))["outcome"] == "done"
        assert w2.last_hello["resume"] is False and w2.sync.runs() == [0, 1]
        assert verify_session(session.root) == []
    finally:
        await w2.close()
