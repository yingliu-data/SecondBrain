"""temporal — the Temporal client and worker of this agent-api.

  start(activities, tenants_doc)   connect (TEMPORAL_ADDRESS), run
                                   flows.worker.build_worker on this loop with
                                   the real activities, ensure_schedules; a
                                   failed connect logs and leaves the direct path
  start_in_background(...)         start() as a task: startup never waits for
                                   Temporal; /chat takes the direct path until
                                   `client` is set (the worker runs)
  stop()                           shut the worker down
  client                           the connected Client, or None (direct path)
  status(ref)                      the session workflow's `status` query, or None
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging

from temporalio.client import Client
from temporalio.service import RPCError

from flows.activities import SessionRef
from flows.client import query_status
from flows.schedules import ensure_schedules
from flows.session_workflow import Status
from flows.worker import build_worker

from app.config import (TEMPORAL_ADDRESS, TEMPORAL_CONNECT_TIMEOUT_S, TEMPORAL_NAMESPACE,
                        TEMPORAL_TASK_QUEUE)

logger = logging.getLogger("host.temporal")

client: Client | None = None
task_queue: str = TEMPORAL_TASK_QUEUE
_worker = None
_worker_task: asyncio.Task | None = None
_start_task: asyncio.Task | None = None


def enabled() -> bool:
    return client is not None


async def start(activities: list, tenants_doc: dict | None = None, *,
                address: str = TEMPORAL_ADDRESS, namespace: str = TEMPORAL_NAMESPACE,
                queue: str = TEMPORAL_TASK_QUEUE, connected: Client | None = None,
                **worker_kwargs) -> bool:
    """True when the worker runs. `connected` skips the connect (tests)."""
    global client, task_queue, _worker, _worker_task
    if connected is None:
        if not address:
            return False
        try:
            connected = await asyncio.wait_for(Client.connect(address, namespace=namespace),
                                               timeout=TEMPORAL_CONNECT_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - any connect failure keeps the direct path
            logger.error("Temporal at %s (%s) unreachable, direct path stays: %r",
                         address, namespace, e)
            return False
    _worker = build_worker(connected, task_queue=queue, activities=activities, **worker_kwargs)
    _worker_task = asyncio.get_running_loop().create_task(_worker.run(),
                                                          name=f"temporal-worker:{queue}")
    _worker_task.add_done_callback(_worker_exited)
    client, task_queue = connected, queue
    logger.info("Temporal worker on %s (%s)", queue, connected.namespace)
    if tenants_doc is not None:
        try:
            result = await ensure_schedules(connected, tenants_doc, task_queue=queue)
            logger.info("schedules: %s", result)
        except Exception as e:  # noqa: BLE001 - schedules never block startup
            logger.error("ensure_schedules failed: %r", e)
    return True


def start_in_background(activities: list, tenants_doc: dict | None = None,
                        **kwargs) -> asyncio.Task:
    """start() off the startup path (connect, worker, ensure_schedules)."""
    global _start_task
    _start_task = asyncio.get_running_loop().create_task(
        start(activities, tenants_doc, **kwargs), name="temporal-start")
    return _start_task


def _worker_exited(t: asyncio.Task) -> None:
    """A worker that dies on its own (namespace missing, poll failure)
    would leave /chat signalling workflows no one runs: back to the direct
    path. stop() clears _worker_task first, so its shutdown is not this."""
    global client, _worker, _worker_task
    if t is not _worker_task:
        return
    e = None if t.cancelled() else t.exception()
    logger.error("Temporal worker exited (%r), direct path from now on", e)
    client, _worker, _worker_task = None, None, None


async def stop() -> None:
    global client, _worker, _worker_task, _start_task
    if _start_task is not None and not _start_task.done():
        _start_task.cancel()
    _start_task = None
    w, t = _worker, _worker_task
    client, _worker, _worker_task = None, None, None
    if w is not None:
        try:
            await w.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning("Temporal worker shutdown: %r", e)
    if t is not None:
        try:
            await t
        except Exception:  # noqa: BLE001
            pass


async def status(ref: SessionRef) -> Status | None:
    """The workflow's `status` query; None when Temporal is off or the
    session has no workflow."""
    if client is None:
        return None
    try:
        return await query_status(client, ref)
    except Exception as e:  # noqa: BLE001 - not found, closed, no worker to answer
        if not isinstance(e, RPCError):
            logger.info("status query %s/%s/%s: %r", ref.user, ref.tenant, ref.session_id, e)
        return None


def status_dict(st: Status | None) -> dict | None:
    return dataclasses.asdict(st) if st is not None else None
