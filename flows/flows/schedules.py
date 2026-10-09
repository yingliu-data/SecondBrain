"""Temporal Schedules from ``tenants.json``.

Each tenant may carry::

    "schedules": [{"cron": "0 7 * * *", "prompt": "Morning brief", "session": "daily",
                   "tz": "Europe/London"}]

``cron`` and ``prompt`` are required; ``session`` defaults to ``daily``
(``DEFAULT_SESSION``) and ``tz`` to ``UTC``.  The fire goes to the session
``{tenant.user or tenant.name}/{tenant.name}/{session}`` with the tenant's
``SessionConfig.from_tenant``.  A malformed entry is logged and skipped, so
one bad line never stops agent-api's startup.

``ensure_schedules`` creates or (when changed) updates one Schedule per
entry, id ``{tenant}/{session}/{index}``.  The action starts ``ScheduleTickWorkflow``
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
import hashlib
import json
import logging
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass, field, replace
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
    ScheduleUpdateInput,
)

from flows import TASK_QUEUE
from flows.activities import SessionConfig, SessionRef, SignalSession, TaskSignal
from flows.client import signal_task
from flows.session_workflow import ScheduleTickWorkflow

log = logging.getLogger(__name__)

DEFAULT_SESSION = "daily"
"""Session a schedule entry fires into when it names none."""

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
    """Flatten ``tenants_doc["tenants"][*]["schedules"]`` into entries.

    The index in the id is the entry's position in the tenant's list, so a
    skipped (malformed) entry does not renumber the ones after it.
    """
    out: list[ScheduleEntry] = []
    for tenant in tenants_doc.get("tenants", []):
        name = tenant.get("name")
        schedules = tenant.get("schedules") or []
        if not schedules:
            continue
        if not name:
            log.warning("schedules of a tenant without a name skipped")
            continue
        user = tenant.get("user") or name  # agent-api's Tenant model defaults user to name
        try:
            config = SessionConfig.from_tenant(tenant)
        except (TypeError, ValueError) as e:
            log.warning("tenant %s: bad session config, schedules skipped: %s", name, e)
            continue
        for i, s in enumerate(schedules):
            if not (isinstance(s, dict) and s.get("cron") and s.get("prompt")):
                log.warning("tenant %s: schedule %d needs cron and prompt, skipped: %r", name, i, s)
                continue
            out.append(ScheduleEntry(
                tenant=name, user=user, index=i, cron=s["cron"], prompt=s["prompt"],
                session=s.get("session") or DEFAULT_SESSION, tz=s.get("tz") or "UTC",
                config=config,
            ))
    return out


FINGERPRINT_MEMO = "fingerprint"
"""The one memo key of a schedule's action: the hash of everything
``_schedule_for`` builds it from (``_fingerprint``)."""

POLICY = SchedulePolicy(
    overlap=ScheduleOverlapPolicy.SKIP, catchup_window=CATCHUP_WINDOW, pause_on_failure=False,
)
"""Every schedule's policy: missed fires and overlapping fires are skipped."""


def _fingerprint(entry: ScheduleEntry, task_queue: str) -> str:
    """sha256 over every input of ``_schedule_for``.  The server stores the cron
    compiled to a calendar, so a schedule read back never equals the one we
    built; comparing this hash (kept in the action's memo) instead tells
    whether a rewrite is needed."""
    inputs = {
        "id": entry.schedule_id, "workflow": ScheduleTickWorkflow.__name__,
        "task_queue": task_queue, "user": entry.user, "tenant": entry.tenant,
        "session": entry.session, "cron": entry.cron, "tz": entry.tz,
        "prompt": entry.prompt, "config": asdict(entry.config),
        "policy": [POLICY.overlap.name, POLICY.catchup_window.total_seconds(),
                   POLICY.pause_on_failure],
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def _schedule_for(entry: ScheduleEntry, task_queue: str, fingerprint: str) -> Schedule:
    """The schedule for `entry`; `fingerprint` (``_fingerprint``) goes in its memo."""
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
            memo={FINGERPRINT_MEMO: fingerprint},
        ),
        spec=ScheduleSpec(cron_expressions=[entry.cron], time_zone_name=entry.tz),
        policy=POLICY,
    )


async def _stored_fingerprint(client: Client, current: Schedule) -> str | None:
    """The fingerprint memo of a schedule read back from the server (``None``
    for one written before fingerprints, or not by us). Decoded with the
    client's full data converter: the memo was encoded with it (codec too)."""
    memo = getattr(current.action, "memo", None) or {}
    payload = memo.get(FINGERPRINT_MEMO)
    if payload is None:
        return None
    try:
        return (await client.data_converter.decode([payload], [str]))[0]
    except Exception:  # noqa: BLE001 - an unreadable memo is a changed schedule
        return None


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

    An existing schedule is rewritten only when its stored fingerprint
    (``_fingerprint``) differs from the entry's, so a restart does not touch
    unchanged ones (nor resume one paused by hand); one without a fingerprint
    is rewritten once.

    Returns ``{schedule_id: "created" | "updated" | "unchanged" | "deleted"}``.
    """
    if isinstance(entries, dict):
        tenants = tenants or {t["name"] for t in entries.get("tenants", []) if t.get("name")}
        entries = entries_from_tenants(entries)
    wanted = {e.schedule_id: e for e in entries}
    existing = {s.id async for s in await client.list_schedules()}

    async def _upsert(sid: str, entry: ScheduleEntry) -> tuple[str, str]:
        fingerprint = _fingerprint(entry, task_queue)
        schedule = _schedule_for(entry, task_queue, fingerprint)
        if sid not in existing:
            try:
                await client.create_schedule(sid, schedule)
                return sid, "created"
            except ScheduleAlreadyRunningError:
                pass  # created since the listing (visibility lag)
        changed = False

        async def _update(inp: ScheduleUpdateInput) -> ScheduleUpdate | None:
            nonlocal changed
            if await _stored_fingerprint(client, inp.description.schedule) == fingerprint:
                return None  # the SDK skips the write
            changed = True
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(sid).update(_update)
        return sid, "updated" if changed else "unchanged"

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
