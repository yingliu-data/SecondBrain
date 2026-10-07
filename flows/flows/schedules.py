"""Temporal Schedules from ``tenants.json`` (PLAN section 10, "Schedules").

Each tenant may carry::

    "schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily",
                   "tz": "Europe/London"}]

``ensure_schedules`` creates or updates one Schedule per entry, id
``{tenant}/{session}/{index}``.  The action starts ``ScheduleTickWorkflow``
(workflow id ``{schedule-id}-{fire-time}``, Temporal appends the fire time),
which runs the ``signal_session`` activity below: a signal-with-start of the
session workflow ``{user}/{tenant}/{session}`` with a ``task`` of kind
``schedule``.  Missed fires are skipped (catchup window = minimum, overlap =
SKIP).

Stale schedules (``{tenant}/...`` ids of a known tenant that are no longer
listed) are deleted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
)

from flows import TASK_QUEUE
from flows.activities import SessionConfig, SessionRef, SignalSession, TaskSignal
from flows.client import signal_task
from flows.session_workflow import ScheduleTickWorkflow

CATCHUP_WINDOW = timedelta(seconds=10)
"""Smallest window the server accepts; a fire missed while the server was
down is run only if it is less than this old, i.e. effectively 'skip'."""


@dataclass(frozen=True)
class ScheduleEntry:
    tenant: str
    user: str
    index: int
    cron: str
    prompt: str
    session: str
    tz: str = "UTC"
    config: SessionConfig = field(default_factory=SessionConfig)
    """The tenant's session config, for the session the fire may start."""

    @property
    def schedule_id(self) -> str:
        return schedule_id(self.tenant, self.session, self.index)


def schedule_id(tenant: str, session: str, index: int) -> str:
    return f"{tenant}/{session}/{index}"


def entries_from_tenants(tenants_doc: dict[str, Any]) -> list[ScheduleEntry]:
    """Flatten ``tenants_doc["tenants"][*]["schedules"]`` into entries."""
    out: list[ScheduleEntry] = []
    for tenant in tenants_doc.get("tenants", []):
        name = tenant["name"]
        user = tenant.get("user") or name  # agent-api's Tenant model defaults user to name
        config = SessionConfig.from_tenant(tenant)
        for i, s in enumerate(tenant.get("schedules", []) or []):
            out.append(ScheduleEntry(
                tenant=name, user=user, index=i, cron=s["cron"], prompt=s["prompt"],
                session=s["session"], tz=s.get("tz", "UTC"), config=config,
            ))
    return out


def _schedule_for(entry: ScheduleEntry, task_queue: str) -> Schedule:
    session = SessionRef(user=entry.user, tenant=entry.tenant, session_id=entry.session)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ScheduleTickWorkflow.run,
            SignalSession(session=session, task=TaskSignal(input_id="", text=entry.prompt,
                                                           kind="schedule"),
                          task_queue=task_queue, config=entry.config),
            id=entry.schedule_id,
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=entry.config.tick_timeout_s),
        ),
        spec=ScheduleSpec(cron_expressions=[entry.cron], time_zone_name=entry.tz),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=CATCHUP_WINDOW,
            pause_on_failure=False,
        ),
    )


def _is_ours(sid: str, tenants: Collection[str]) -> bool:
    parts = sid.split("/")
    return len(parts) == 3 and parts[0] in tenants and parts[2].isdigit()


async def ensure_schedules(
    client: Client,
    entries: Sequence[ScheduleEntry] | dict[str, Any],
    *,
    task_queue: str = TASK_QUEUE,
    delete_stale: bool = True,
    tenants: Collection[str] | None = None,
) -> dict[str, str]:
    """Create or update one Schedule per entry; delete the stale ones.

    ``entries`` are prepared ``ScheduleEntry`` objects (agent-api builds them
    from its TenantRegistry) or the ``tenants.json`` document itself.
    ``tenants`` scopes the stale deletion (default: every tenant in the doc,
    or every tenant that has an entry).

    Returns ``{schedule_id: "created" | "updated" | "deleted"}``.
    """
    if isinstance(entries, dict):
        tenants = tenants or {t["name"] for t in entries.get("tenants", [])}
        entries = entries_from_tenants(entries)
    wanted = {e.schedule_id: e for e in entries}
    existing = {s.id async for s in await client.list_schedules()}

    async def _upsert(sid: str, entry: ScheduleEntry) -> tuple[str, str]:
        schedule = _schedule_for(entry, task_queue)
        if sid not in existing:
            try:
                await client.create_schedule(sid, schedule)
                return sid, "created"
            except ScheduleAlreadyRunningError:
                pass  # created since the listing (visibility lag)
        await client.get_schedule_handle(sid).update(lambda _inp: ScheduleUpdate(schedule=schedule))
        return sid, "updated"

    async def _delete(sid: str) -> tuple[str, str]:
        await client.get_schedule_handle(sid).delete()
        return sid, "deleted"

    stale: list[str] = []
    if delete_stale:
        scope = tenants if tenants is not None else {e.tenant for e in entries}
        stale = [sid for sid in existing if sid not in wanted and _is_ours(sid, scope)]
    return dict(await asyncio.gather(
        *(_upsert(sid, e) for sid, e in wanted.items()),
        *(_delete(sid) for sid in stale),
    ))


class SessionSignaller:
    """Holds the client for the ``signal_session`` glue activity.

    Registered by ``flows.worker.start_worker`` next to the four session
    activities.  It is the only activity that talks to Temporal itself.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name="signal_session")
    async def signal_session(self, inp: SignalSession) -> str:
        # The tick workflow's id is ``{schedule-id}-{fire-time}``: unique per
        # fire, stable across retries, so the session workflow dedupes on it.
        task = replace(inp.task, input_id=activity.info().workflow_id)
        await signal_task(self._client, inp.session, task,
                          task_queue=inp.task_queue, config=inp.config)
        return task.input_id
