"""``ensure_schedules`` against a real dev server.

The Java time-skipping test server has no schedule support, so this test runs
on the ``tenv`` fixture only when it is a real server (``start_local`` or an
external one); on the time-skipping env it starts its own ``start_local``
dev server, and skips if that cannot be launched.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio.testing import WorkflowEnvironment

from flows.activities import SessionRef
from flows.schedules import ensure_schedules, entries_from_tenants, schedule_id


async def test_entries_from_tenants():
    doc = {"tenants": [
        {"name": "default", "user": "alice", "answer_wait_s": 60, "schedules": [
            {"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily", "tz": "Europe/London"},
            {"cron": "*/30 * * * *", "prompt": "Inbox sweep", "session": "daily"},
        ]},
        {"name": "wcc-event"},  # no schedules
    ]}
    entries = entries_from_tenants(doc)
    assert [e.schedule_id for e in entries] == ["default/daily/0", "default/daily/1"]
    assert entries[0].user == "alice" and entries[0].tz == "Europe/London"
    assert entries[0].config.answer_wait_s == 60  # the tenant's config rides along
    assert entries[1].tz == "UTC"
    assert schedule_id("t", "s", 3) == "t/s/3"


async def test_ensure_schedules_create_update_trigger_delete(tenv, make_session):
    own_env = None
    if tenv.skips_time:
        try:
            own_env = await WorkflowEnvironment.start_local()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no dev server for schedules: {e!r}")
        client = own_env.client
    else:
        client = tenv.client
    try:
        uid = uuid.uuid4().hex[:8]
        tenant = f"t-{uid}"
        tq = f"sched-{uid}"
        doc = {"tenants": [{"name": tenant, "user": "alice", "schedules": [
            {"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily", "tz": "Europe/London"},
            {"cron": "0 8 * * *", "prompt": "Second", "session": "daily"},
        ]}]}
        sid0 = schedule_id(tenant, "daily", 0)
        sid1 = schedule_id(tenant, "daily", 1)

        res = await ensure_schedules(client, doc, task_queue=tq)
        assert res == {sid0: "created", sid1: "created"}

        desc = await client.get_schedule_handle(sid0).describe()
        # the server stores a cron expression as a structured calendar
        cal = desc.schedule.spec.calendars[0]
        assert (cal.hour[0].start, cal.minute[0].start) == (7, 0)
        assert desc.schedule.spec.time_zone_name == "Europe/London"
        assert desc.schedule.action.workflow == "ScheduleTickWorkflow"
        assert desc.schedule.policy.catchup_window == timedelta(seconds=10)

        # second call (prepared entries this time) updates in place and
        # removes the entry no longer listed
        doc["tenants"][0]["schedules"] = [
            {"cron": "30 7 * * *", "prompt": "Morning brief", "session": "daily", "tz": "Europe/London"},
        ]
        res = await ensure_schedules(client, entries_from_tenants(doc), task_queue=tq)
        assert res == {sid0: "updated", sid1: "deleted"}
        desc = await client.get_schedule_handle(sid0).describe()
        cal = desc.schedule.spec.calendars[0]
        assert (cal.hour[0].start, cal.minute[0].start) == (7, 30)

        # a fire: ScheduleTickWorkflow -> signal_session -> SessionWorkflow(kind=schedule)
        ref = SessionRef(user="alice", tenant=tenant, session_id="daily")
        s = await make_session(client=client, ref=ref, task_queue=tq)
        await client.get_schedule_handle(sid0).trigger()
        await s.wait_delivered(1)
        runs = s.calls("run_task")
        assert len(runs) == 1
        assert runs[0]["input"].kind == "schedule"
        assert runs[0]["input"].text == "Morning brief"
        assert runs[0]["input"].input_id.startswith(sid0 + "-")  # {schedule-id}-{fire time}
        assert runs[0]["input"].session == ref
        await s.close()

        await client.get_schedule_handle(sid0).delete()
    finally:
        if own_env is not None:
            await own_env.shutdown()
