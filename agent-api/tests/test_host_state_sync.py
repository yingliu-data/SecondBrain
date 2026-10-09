"""StateSync: append/ack, duplicates and gaps, segments, runs, projections,
host log, replay equality."""
import json

import pytest

from protocol.canonical import sha256_hex
from protocol.fold import fold, state_hash

from app.host.state_sync import StateSync, StateSyncError
from host_fakes import HASH, read_jsonl, session, users_root  # noqa: F401

SID = "s1"
TOOLS = [{"name": "bash", "side": "guest", "pin": None},
         {"name": "web_search", "side": "host", "pin": None},
         {"name": "get_calendar_events", "side": "device", "pin": None}]


def ev(seq, type, body):
    return {"session_id": SID, "seq": seq, "ts": "2026-10-07T12:00:00Z", "schema": 1,
            "type": type, "body": body}


def start(seq=1):
    return ev(seq, "session_start", {
        "user": "u", "tenant": "t", "workflow_run_id": "u/t/s1", "config_hash": HASH,
        "system_hash": HASH, "tools": TOOLS, "harness_version": "x", "image_version": "x",
        "resumed": False})


def text_task(input_id="in_1", text="hi", answer="hello there world"):
    """session_start, input, clock, turn_start, llm_response, settle."""
    return [
        start(),
        ev(2, "input", {"input_id": input_id, "kind": "task", "text": text, "dedupe": "new"}),
        ev(3, "clock", {"kind": "turn_clock", "at": 1}),
        ev(4, "turn_start", {"turn_id": "t1", "type": "regular", "includes_through_seq": 3,
                             "model": "m", "params": {}, "request_hash": HASH,
                             "preamble_hash": HASH, "workspace_tree_id": None}),
        ev(5, "llm_response", {"turn_id": "t1", "finish_reason": "stop",
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                               "message": {"role": "assistant", "content": answer}}),
        ev(6, "settle", {"input_id": input_id, "outcome": "done", "answer": answer,
                         "usage": {"prompt_tokens": 3, "completion_tokens": 2}, "manifest": {}}),
    ]


def test_accept_appends_fsyncs_then_acks(session):
    sync = StateSync(session)
    assert sync.accept(start()) == 1
    assert sync.last_seq == 1
    seg = session.root / "events" / "r0001" / "000001.jsonl"
    assert read_jsonl(seg)[0]["type"] == "session_start"
    assert sync.state.session_id == SID


def test_duplicate_and_gap_are_not_appended(session):
    sync = StateSync(session)
    sync.accept(start())
    events = text_task()
    assert sync.accept(events[1]) == 2
    assert sync.accept(events[1]) == 2            # duplicate: re-ack, no append
    assert sync.accept(events[3]) == 2            # gap (seq 4 before 3): ack stays
    assert len(sync.read(0)) == 2
    assert sync.accept(events[2]) == 3


def test_schema_error_is_refused(session):
    sync = StateSync(session)
    sync.accept(start())
    with pytest.raises(StateSyncError):
        sync.accept(ev(2, "input", {"input_id": "x"}))
    assert sync.last_seq == 1
    # a fold error (tool not offered) is refused too
    sync.accept(ev(2, "input", {"input_id": "i", "kind": "task", "text": "t", "dedupe": "new"}))
    sync.accept(ev(3, "turn_start", {"turn_id": "t1", "type": "regular", "includes_through_seq": 2,
                                     "model": "m", "params": {}, "request_hash": HASH,
                                     "preamble_hash": HASH, "workspace_tree_id": None}))
    with pytest.raises(StateSyncError):
        sync.accept(ev(4, "tool_call", {"turn_id": "t1", "call_id": "c", "name": "nope",
                                        "side": "host", "args": {}, "state": "RUNNING"}))


def test_segments_rotate_and_read_replays_equal(session):
    sync = StateSync(session, segment_events=3)
    events = text_task()
    for e in events:
        sync.accept(e)
    run = session.root / "events" / "r0001"
    assert sorted(p.name for p in run.iterdir()) == ["000001.jsonl", "000002.jsonl"]
    assert [e["seq"] for e in read_jsonl(run / "000002.jsonl")] == [4, 5, 6]
    assert [e["seq"] for e in sync.read(4)] == [5, 6]
    # replay equality: folding the stored log gives the live state
    assert state_hash(fold(sync.read(0))) == state_hash(sync.state)
    # and a fresh StateSync loads the same
    again = StateSync(session, segment_events=3)
    assert again.last_seq == 6
    assert state_hash(again.state) == state_hash(sync.state)
    assert again.settled["in_1"]["answer"] == "hello there world"
    assert again.seen_inputs == {"in_1"}


def test_projections_history_and_trace(session):
    sync = StateSync(session)
    sync.ticket_id = "tk_1"
    for e in text_task():
        sync.accept(e)
    hist = session.read_history()
    assert hist == [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello there world"}]
    names = [t["event"] for t in session.read_trace()]
    assert "assistant_response" in names and "settle" in names
    assert all(t.get("ticket_id") == "tk_1" for t in session.read_trace())


def test_tool_trace_projection(session):
    sync = StateSync(session)
    base = text_task()[:4]
    for e in base:
        sync.accept(e)
    sync.accept(ev(5, "llm_response", {"turn_id": "t1", "finish_reason": "tool_calls", "usage": {},
                                       "message": {"role": "assistant", "content": None,
                                                   "tool_calls": []}}))
    sync.accept(ev(6, "tool_call", {"turn_id": "t1", "call_id": "c1", "name": "web_search",
                                    "side": "host", "args": {"q": "x"}, "state": "RUNNING"}))
    sync.accept(ev(7, "op_state", {"call_id": "c1", "state": "done", "duration_ms": 12}))
    sync.accept(ev(8, "tool_result", {"call_id": "c1", "content": "found"}))
    sync.accept(ev(9, "error", {"class": "transient", "message": "502", "retry": True}))
    sync.accept(ev(10, "error", {"class": "limit", "message": "max_tools", "retry": False}))
    trace = {t["event"]: t for t in session.read_trace()}
    assert trace["tool_call"]["detail"] == {"name": "web_search", "arguments": {"q": "x"},
                                            "call_id": "c1", "side": "host"}
    assert trace["tool_result"]["detail"]["result"] == "found"
    assert trace["tool_result"]["detail"]["duration_ms"] == 12
    assert "llm_error" in trace and "loop_exhausted" in trace


def test_host_event_log_and_runs(session):
    sync = StateSync(session)
    rec = sync.host_event("usage", {"prompt_tokens": 5, "completion_tokens": 1})
    assert rec["hseq"] == 1
    assert read_jsonl(session.root / "events" / "host-000001.jsonl")[0]["type"] == "usage"
    for e in text_task():
        sync.accept(e)
    assert sync.begin_run() == 2
    assert sync.last_seq == 0 and sync.begin_run() == 2       # empty run: no-op
    sync.accept(start())
    assert sync.read(0, run=1)[-1]["type"] == "settle"
    assert (session.root / "events" / "r0002" / "000001.jsonl").exists()
    again = StateSync(session)
    assert again.run == 2 and again.last_seq == 1
    assert again.settled["in_1"]["outcome"] == "done"         # across runs
    assert again.read_host()[0]["body"]["prompt_tokens"] == 5
    assert json.loads(json.dumps(again.state.to_canonical()))  # canonicalisable


# ---------------------------------------------------------------- memory (D23)
def compaction_run(summary="Goal: greet. Progress: said hi.", sha=None):
    """A text task, then a compaction turn (clock, turn_start{compaction},
    llm_response, compaction, memory{session, write}) as the guest emits it."""
    events = text_task()
    events += [
        ev(7, "clock", {"kind": "turn_clock", "at": 2}),
        ev(8, "turn_start", {"turn_id": "t_sum", "type": "compaction", "includes_through_seq": 6,
                             "model": "m", "params": {}, "request_hash": HASH,
                             "preamble_hash": HASH, "workspace_tree_id": None}),
        ev(9, "llm_response", {"turn_id": "t_sum", "finish_reason": "stop",
                               "usage": {"prompt_tokens": 9, "completion_tokens": 4},
                               "message": {"role": "assistant", "content": summary}}),
        ev(10, "compaction", {"range": [2, 6], "summary_turn_id": "t_sum",
                              "tokens_before": 90, "tokens_after": 12}),
        ev(11, "memory", {"scope": "session", "op": "write", "slug": "compaction-8",
                          "sha256": sha or sha256_hex(summary.strip()), "derived_from": "t_sum"}),
    ]
    return events


def test_compaction_memory_is_projected_to_the_session_tier(session, caplog):
    from app.user.memory import MemoryStore

    sync = StateSync(session)
    with caplog.at_level("WARNING", logger="host.state_sync"):
        for e in compaction_run():
            sync.accept(e)
    assert not [r for r in caplog.records if "sha256" in r.getMessage()]
    path = session.memory / "compaction-8.md"
    assert path.exists()
    recs = {r.slug: r for r in MemoryStore(session.memory).list()}
    rec = recs["compaction-8"]
    assert rec.name == "Compaction summary" and rec.type == "project"
    assert rec.description == "summary of the conversation up to seq 6"
    assert rec.body.strip() == "Goal: greet. Progress: said hi."
    assert "compaction-8" in (session.memory / "MEMORY.md").read_text()
    # the fold agrees: the session tier holds the slug with the event's hash
    assert sync.state.session_memory == {"compaction-8": compaction_run()[-1]["body"]["sha256"]}
    # traced like the other events
    mem = [t for t in session.read_trace() if t["event"] == "memory"]
    assert mem[-1]["detail"] == {"scope": "session", "op": "write", "slug": "compaction-8",
                                 "sha256": compaction_run()[-1]["body"]["sha256"],
                                 "derived_from": "t_sum"}
    # a later remove takes the file away
    sync.accept(ev(12, "memory", {"scope": "session", "op": "remove", "slug": "compaction-8",
                                  "sha256": "", "derived_from": "t_sum"}))
    assert not path.exists() and sync.state.session_memory == {}


def test_compaction_memory_hash_mismatch_warns_but_stores(session, caplog):
    sync = StateSync(session)
    with caplog.at_level("WARNING", logger="host.state_sync"):
        for e in compaction_run(sha="f" * 64):
            sync.accept(e)
    assert any("sha256" in r.getMessage() for r in caplog.records)
    assert (session.memory / "compaction-8.md").exists()
    assert sync.last_seq == 11                       # never fatal


def test_tool_derived_memory_is_traced_not_projected(session):
    """A memory event from remember/forget: the host skill already wrote the
    file (Sides logged the host event); the projection only traces it."""
    sync = StateSync(session)
    events = text_task()[:4]
    for e in events:
        sync.accept(e)
    sync.accept(ev(5, "llm_response", {"turn_id": "t1", "finish_reason": "tool_calls", "usage": {},
                                       "message": {"role": "assistant", "content": None,
                                                   "tool_calls": []}}))
    sync.accept(ev(6, "tool_call", {"turn_id": "t1", "call_id": "c_rem", "name": "web_search",
                                    "side": "host", "args": {"q": "x"}, "state": "RUNNING"}))
    sync.accept(ev(7, "tool_result", {"call_id": "c_rem", "content": "{\"status\": \"ok\"}"}))
    sync.accept(ev(8, "memory", {"scope": "session", "op": "write", "slug": "tz",
                                 "sha256": "a" * 64, "derived_from": "c_rem"}))
    assert not (session.memory / "tz.md").exists()
    assert [t["event"] for t in session.read_trace()][-1] == "memory"
    assert sync.state.session_memory == {"tz": "a" * 64}


def test_memory_derived_from_a_regular_turn_is_not_projected(session, caplog):
    sync = StateSync(session)
    for e in text_task():
        sync.accept(e)
    with caplog.at_level("WARNING", logger="host.state_sync"):
        sync.accept(ev(7, "memory", {"scope": "session", "op": "write", "slug": "x",
                                     "sha256": "a" * 64, "derived_from": "t1"}))
    assert not (session.memory / "x.md").exists()
    assert any("not projected" in r.getMessage() for r in caplog.records)


def test_compaction_summary_survives_reload(session):
    """The summary is read from the folded state, so a reloaded StateSync
    projects a memory event that arrives after the reload."""
    sync = StateSync(session)
    for e in compaction_run()[:10]:
        sync.accept(e)
    again = StateSync(session)
    again.accept(compaction_run()[10])
    path = session.memory / "compaction-8.md"
    assert path.exists() and "Goal: greet. Progress: said hi." in path.read_text()
    assert "up to seq 6" in path.read_text()


def test_dirstore_delete_makes_the_host_log_recount(tmp_path):
    """HostLog counts hseq from disk once; DirStore.delete invalidates it, so
    a session re-created under the same id starts again at hseq 1."""
    from app.session.dir_store import DirStore
    store = DirStore(tmp_path / "sessions")
    s = store.get_or_create("u", "t", "s1")
    sync = StateSync(s)
    assert [sync.host_event("vm", {"op": "boot"})["hseq"] for _ in range(2)] == [1, 2]
    assert store.delete("u", "t", "s1")
    s2 = store.get_or_create("u", "t", "s1")
    assert StateSync(s2).host_event("vm", {"op": "boot"})["hseq"] == 1
    assert sync.host_event("vm", {"op": "boot"})["hseq"] == 2      # the same writer


def test_ticket_id_is_per_input_not_per_seq():
    from flows.activities import SessionRef
    from app.host.activities import ticket_id_for
    ref = SessionRef(user="u", tenant="t", session_id="s1")
    a, b = ticket_id_for(ref, "in_a"), ticket_id_for(ref, "in_b")
    assert a != b and a == ticket_id_for(ref, "in_a")
    assert a.startswith("ticket_") and len(a) == len("ticket_") + 24
    assert ticket_id_for(SessionRef(user="u", tenant="t", session_id="s2"), "in_a") != a


def test_read_current_run_without_loading_the_session(session):
    """GET /events with no live worker: the latest run only, no fold."""
    from app.host.state_sync import read_current_run
    sync = StateSync(session)
    sync.accept(start())
    sync.begin_run()
    sync.accept(start())
    assert sync.run == 2 and read_current_run(sync.events_dir, 0) == sync.read(0) == [start()]
    assert read_current_run(sync.events_dir, 1) == []
    assert read_current_run(session.root / "nothing", 0) == []
