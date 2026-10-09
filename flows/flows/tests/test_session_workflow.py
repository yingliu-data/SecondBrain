"""SessionWorkflow behaviour on the Temporal test server (PLAN section 10)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from flows.activities import SessionConfig, SessionRef, TaskSignal
from flows.client import session_handle, signal_abort, signal_answer
from flows.session_workflow import (
    AnswerSignal,
    InFlight,
    SessionState,
    SessionWorkflow,
    parse_workflow_id,
    workflow_id,
)


def test_workflow_id_roundtrip():
    wid = workflow_id("alice", "default", "2026-10-07T07-00-00")
    assert wid == "alice/default/2026-10-07T07-00-00"
    assert parse_workflow_id(wid) == SessionRef("alice", "default", "2026-10-07T07-00-00")


def test_session_config_from_tenant():
    cfg = SessionConfig.from_tenant({"name": "default", "wall_clock_s": 900, "answer_wait_s": 60})
    assert (cfg.wall_clock_s, cfg.answer_wait_s, cfg.max_tasks_per_run) == (900, 60, 50)
    assert cfg.activities["run_task"].timeout_s == 900
    assert cfg.activities["run_task"].heartbeat_s == 120  # untouched
    assert SessionConfig.from_tenant({"name": "bare"}) == SessionConfig()
    assert SessionConfig.from_tenant({"max_tasks_per_run": 2}).max_tasks_per_run == 2


def test_session_config_from_tenant_edges():
    # null keys keep the defaults; 0 answer wait is allowed (deny path)
    assert SessionConfig.from_tenant({"wall_clock_s": None, "answer_wait_s": None}) == SessionConfig()
    assert SessionConfig.from_tenant({"answer_wait_s": 0}).answer_wait_s == 0
    assert SessionConfig.from_tenant({"answer_wait_s": "86400"}).answer_wait_s == 86400
    assert SessionConfig.from_tenant({"max_history_events": 500}).max_history_events == 500
    assert SessionConfig().max_history_events == 10_000
    for bad in ({"wall_clock_s": 0}, {"max_tasks_per_run": -1}, {"answer_wait_s": -5}):
        with pytest.raises(ValueError):
            SessionConfig.from_tenant({"name": "t", **bad})


async def test_a_duplicate_task_signal_runs_once(make_session):
    s = await make_session()
    t = TaskSignal(input_id="in-1", text="hello")
    await s.send(t)
    await s.send(t)  # redelivered POST
    await s.wait_delivered(1)
    st = await s.wait_state("idle")
    await asyncio.sleep(0.5)  # give a wrongly-duplicated run time to show up
    assert len(s.calls("run_task")) == 1
    assert len(s.calls("deliver")) == 1
    assert s.calls("deliver")[0]["input"].outcome == "done"
    assert st.seen == 1 and st.tasks_run == 1


async def test_a2_scripted_outcome_is_delivered(make_session):
    s = await make_session()
    s.script("in-f", outcome="failed")
    await s.send(TaskSignal(input_id="in-f", text="fail"))
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "failed"
    assert s.calls("run_task")[0]["input"].wall_clock_s == 600  # config default


async def test_b_parked_then_answer_resumes(make_session):
    s = await make_session()
    s.script("in-q", park=True)
    await s.send(TaskSignal(input_id="in-q", text="ask me"))
    st = await s.wait_state("parked")
    assert st.question_id == "q-in-q" and st.current_input_id == "in-q"

    await signal_answer(s.client, s.ref, AnswerSignal(question_id="q-in-q", text="42"))
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "done"

    runs = s.calls("run_task")
    assert len(runs) == 2
    assert runs[0]["input"].resume is False
    assert runs[1]["input"].resume is True and runs[1]["input"].answer == "42"
    preps = s.calls("prepare_vm")
    assert [p["input"].resume_from_park for p in preps] == [False, True]
    await s.wait_state("idle")


async def test_b2_answer_for_wrong_question_is_ignored(make_session):
    s = await make_session()
    s.script("in-q", park=True)
    await s.send(TaskSignal(input_id="in-q", text="ask me"))
    await s.wait_state("parked")
    await signal_answer(s.client, s.ref, AnswerSignal(question_id="other", text="no"))
    await asyncio.sleep(0.3)
    assert (await s.status()).state == "parked"
    await signal_answer(s.client, s.ref, AnswerSignal(question_id="q-in-q", text="yes"))
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "done"


async def test_b3_carried_parked_task_resumes_in_new_run(make_session):
    """A run started with ``in_flight`` (continue-as-new while parked) parks
    the VM again, then waits for the answer like a freshly parked task."""
    s = await make_session()
    task = TaskSignal(input_id="in-q", text="ask me")
    s.script("in-q", park=True)
    await s.client.start_workflow(
        SessionWorkflow.run,
        SessionState(session=s.ref, seen=["in-q"], generation=1,
                     in_flight=InFlight(task, "q-in-q")),
        id=workflow_id(s.ref.user, s.ref.tenant, s.ref.session_id),
        task_queue=s.task_queue,
    )
    st = await s.wait_state("parked")
    assert st.question_id == "q-in-q" and st.generation == 1
    # the status says parked before park_vm has run (so an early answer is not
    # dropped); wait for the call itself before counting
    await s.wait_for(lambda: len(s.calls("park_vm")) == 1, what="park_vm called")
    assert s.calls("run_task") == []

    await signal_answer(s.client, s.ref, AnswerSignal(question_id="q-in-q", text="42"))
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "done"
    assert [p["input"].resume_from_park for p in s.calls("prepare_vm")] == [True]
    assert [r["input"].resume for r in s.calls("run_task")] == [True]
    st = await s.wait_state("idle")
    assert st.tasks_run == 1 and st.seen == 1


async def test_c_parked_timer_expiry_escalates(make_session, tenv):
    # 24 h wait on the time-skipping server; 2 s real wait on the fallbacks.
    wait_s = 24 * 3600 if tenv.skips_time else 2
    s = await make_session(SessionConfig(answer_wait_s=wait_s))
    s.script("in-q", park=True)
    await s.send(TaskSignal(input_id="in-q", text="ask me"))
    await s.wait_state("parked")
    # parked is reported before park_vm runs (an early answer must not be lost)
    await s.wait_for(lambda: len(s.calls("park_vm")) == 1, what="park_vm called")

    if tenv.skips_time:
        await tenv.env.sleep(timedelta(seconds=wait_s + 1))
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "escalated"
    assert delivered[0]["input"].question_id == "q-in-q"
    assert len(s.calls("run_task")) == 1  # never resumed
    await s.wait_state("idle")


async def test_d_run_task_retry_receives_heartbeat_details(make_session):
    s = await make_session()
    s.script("in-crash", fail_once=True)
    await s.send(TaskSignal(input_id="in-crash", text="crash once"))
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "done"

    runs = s.calls("run_task")
    assert [r["attempt"] for r in runs] == [1, 2]
    assert runs[0]["heartbeat_details"] == []
    crashed_at = runs[0]["crashed_at_seq"]
    details = runs[1]["heartbeat_details"]
    assert details, "retry attempt saw no heartbeat details"
    # The SDK throttles heartbeats (first one immediately, then one per
    # throttle interval); Core flushes the last pending heartbeat before
    # reporting the failure, so the retry sees the LAST seq, not the first.
    assert details == [crashed_at], (details, crashed_at)
    assert len(s.calls("prepare_vm")) == 1  # the retry is the activity's, not the workflow's


async def test_e_continue_as_new_after_n_tasks(make_session):
    s = await make_session(SessionConfig(max_tasks_per_run=2))
    h = await s.send(TaskSignal(input_id="in-1", text="one"))
    first_run = h.result_run_id or (await h.describe()).run_id
    await s.send(TaskSignal(input_id="in-2", text="two"))
    await s.send(TaskSignal(input_id="in-3", text="three"))
    await s.wait_delivered(3)
    st = await s.wait_state("idle")
    assert st.generation == 1
    assert st.seen == 3  # carried across continue-as-new
    assert st.tasks_run == 1  # one task in the new run
    desc = await session_handle(s.client, s.ref).describe()
    assert desc.run_id != first_run

    # dedupe survives continue-as-new
    await s.send(TaskSignal(input_id="in-1", text="one again"))
    await asyncio.sleep(0.5)
    assert len(s.calls("run_task")) == 3


async def test_f_status_query(make_session):
    s = await make_session()
    await s.send(TaskSignal(input_id="in-1", text="go"))
    await s.wait_for(lambda: len(s.calls("run_task")) == 1, what="run_task start")
    st = await s.status()
    assert st.state == "running" and st.current_input_id == "in-1" and st.question_id is None
    await s.wait_delivered(1)
    st = await s.wait_state("idle")
    assert st.current_input_id is None
    assert st.seen == 1 and st.pending == 0 and st.tasks_run == 1 and st.generation == 0


async def test_g_abort_while_running(make_session):
    s = await make_session()
    await s.send(TaskSignal(input_id="in-1", text="long", wall_clock_s=60))
    await s.wait_for(lambda: len(s.calls("run_task")) == 1, what="run_task start")
    assert s.calls("run_task")[0]["input"].wall_clock_s == 60  # the task's override
    await signal_abort(s.client, s.ref, "user cancelled")
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "aborted"
    await s.wait_state("idle")


async def test_h_schedule_kind_task(make_session):
    s = await make_session()
    await s.send(TaskSignal(input_id="default/daily/0-2026-10-07T07:00:00Z", text="Morning brief", kind="schedule"))
    await s.wait_delivered(1)
    assert s.calls("run_task")[0]["input"].kind == "schedule"


async def test_b4_abort_while_parked_delivers_aborted(make_session):
    s = await make_session()
    s.script("in-q", park=True)
    await s.send(TaskSignal(input_id="in-q", text="ask me"))
    await s.wait_state("parked")
    await signal_abort(s.client, s.ref, "user cancelled")
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "aborted"
    assert delivered[0]["input"].question_id == "q-in-q"
    assert len(s.calls("run_task")) == 1  # never resumed
    assert len(s.calls("prepare_vm")) == 1
    await s.wait_state("idle")
    # a late answer for the aborted question is ignored, the session stays usable
    await signal_answer(s.client, s.ref, AnswerSignal(question_id="q-in-q", text="late"))
    await s.send(TaskSignal(input_id="in-2", text="next"))
    delivered = await s.wait_delivered(2)
    assert delivered[1]["input"].outcome == "done"
    assert len(s.calls("run_task")) == 2


async def test_b5_carried_expired_deadline_escalates(make_session):
    """A carried parked task whose deadline already passed escalates after
    re-parking, without waiting a fresh ``answer_wait_s``."""
    s = await make_session()
    task = TaskSignal(input_id="in-q", text="ask me")
    await s.client.start_workflow(
        SessionWorkflow.run,
        SessionState(session=s.ref, seen=["in-q"], generation=1,
                     in_flight=InFlight(task, "q-in-q", deadline=0.0)),  # 1970: long past
        id=workflow_id(s.ref.user, s.ref.tenant, s.ref.session_id),
        task_queue=s.task_queue,
    )
    delivered = await s.wait_delivered(1)
    assert delivered[0]["input"].outcome == "escalated"
    assert len(s.calls("park_vm")) == 1
    assert s.calls("run_task") == []
    await s.wait_state("idle")


N_QUEUED = 8


async def _park_then_hop(s) -> None:
    """Park ``in-q``, then send ``N_QUEUED`` tasks: enough to push a run past
    ``max_history_events=40`` once (about 4 events per signal on top of the
    ~24 of a parked run) but not twice.  No status queries here: on the Java
    test server a query right after a hop can time out (seen after
    ``env.sleep``), so the hop is observed through ``describe`` and the
    re-park."""
    first_run = await s.run_id()
    for i in range(N_QUEUED):
        await s.send(TaskSignal(input_id=f"in-{i}", text=f"queued {i}"))
    await s.wait_for(lambda: len(s.calls("park_vm")) == 2, what="re-park in the new run")
    assert await s.run_id() != first_run
    assert len(s.calls("run_task")) == 1  # the parked task was carried, not re-run


async def test_b6_history_hop_while_parked_then_answer(make_session):
    s = await make_session(SessionConfig(max_history_events=40))
    s.script("in-q", park=True)
    await s.send(TaskSignal(input_id="in-q", text="ask me"))
    await s.wait_state("parked")
    await _park_then_hop(s)

    await signal_answer(s.client, s.ref, AnswerSignal(question_id="q-in-q", text="42"))
    delivered = await s.wait_delivered(1 + N_QUEUED)
    assert delivered[0]["input"].input_id == "in-q"
    assert all(d["input"].outcome == "done" for d in delivered)
    assert sorted(d["input"].input_id for d in delivered[1:]) == sorted(f"in-{i}" for i in range(N_QUEUED))
    runs = s.calls("run_task")
    assert runs[1]["input"].resume is True and runs[1]["input"].answer == "42"
    assert len(runs) == 2 + N_QUEUED  # in-q: first run + one resume; each queued task once
    st = await s.wait_state("idle")
    assert st.generation >= 1 and st.seen == 1 + N_QUEUED


async def test_b7_hop_while_parked_keeps_the_deadline(make_session, tenv):
    if not tenv.skips_time:
        pytest.skip("needs the time-skipping server")
    s = await make_session(SessionConfig(answer_wait_s=3600, max_history_events=40))
    s.script("in-q", park=True)
    await s.send(TaskSignal(input_id="in-q", text="ask me"))
    await s.wait_state("parked")
    await tenv.env.sleep(timedelta(minutes=40))
    assert s.calls("deliver") == []
    await _park_then_hop(s)
    # A re-armed wait would need 60 more minutes; the carried deadline is 20 away.
    await tenv.env.sleep(timedelta(minutes=21))
    delivered = await s.wait_delivered(1 + N_QUEUED, timeout=30)
    assert delivered[0]["input"].input_id == "in-q"
    assert delivered[0]["input"].outcome == "escalated"
    assert len(s.calls("run_task")) == 1 + N_QUEUED  # in-q never resumed


async def test_e2_continue_as_new_on_history_length(make_session):
    s = await make_session(SessionConfig(max_history_events=30))
    for i in range(4):
        await s.send(TaskSignal(input_id=f"in-{i}", text=str(i)))
    await s.wait_delivered(4)
    st = await s.wait_state("idle")
    assert st.generation >= 1
    assert st.seen == 4
    assert len(s.calls("run_task")) == 4

