"""temporal — the Temporal client and worker of this agent-api.

The path of an input is configuration, never liveness: TEMPORAL_ADDRESS set
-> every input goes through the session workflow; unset -> the direct path
(Worker.act) only. A session never switches path mid-way.

  configured()                     TEMPORAL_ADDRESS set (or start() got a
                                   connected client): the workflow path
  ready()                          the client is connected and the worker runs
  wait_ready(timeout_s)            ready() within timeout_s (POST /chat waits
                                   TEMPORAL_READY_WAIT_S, then answers 503 +
                                   Retry-After)
  start(activities, tenants_doc)   connect, run flows.worker.build_worker on
                                   this loop with the real activities
                                   (max_concurrent_activities from the task
                                   cap), ensure_schedules; False when the
                                   connect fails (requests answer 503)
  start_in_background(...)         a supervisor task: start(), and again after
                                   TEMPORAL_RECONNECT_S whenever the connect
                                   failed or the worker exited; startup never
                                   waits for Temporal
  stop()                           shut the supervisor and the worker down
  client                           the connected Client, or None (not ready)
  status(ref)                      the session workflow's `status` query, or None
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

from temporalio.client import Client
from temporalio.service import RPCError

from flows.activities import SessionRef
from flows.client import query_status
from flows.schedules import ensure_schedules
from flows.session_workflow import Status
from flows.worker import build_worker

from app.config import (HARNESS_MAX_CONCURRENT_TASKS, TEMPORAL_ADDRESS,
                        TEMPORAL_CONNECT_TIMEOUT_S, TEMPORAL_NAMESPACE, TEMPORAL_RECONNECT_S,
                        TEMPORAL_TASK_QUEUE)

logger = logging.getLogger("host.temporal")

client: Client | None = None
task_queue: str = TEMPORAL_TASK_QUEUE
_address: str = TEMPORAL_ADDRESS      # the configured path (not liveness)
_worker = None
_worker_task: asyncio.Task | None = None
_start_task: asyncio.Task | None = None


def configured() -> bool:
    """The workflow path is configured: every input goes through it."""
    return bool(_address)


def ready() -> bool:
    """Connected, and the worker runs (inputs can be signalled)."""
    return client is not None


async def wait_ready(timeout_s: float) -> bool:
    """ready() now or within `timeout_s` (Temporal connecting, or its
    worker being restarted)."""
    deadline = time.monotonic() + timeout_s
    while not ready():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def max_concurrent_activities(cap: int = HARNESS_MAX_CONCURRENT_TASKS) -> int:
    """The worker's activity slots: room for the cap's running run_tasks
    plus as many queued on the Worker's admission, and the short
    activities (prepare_vm, park_vm, deliver, signal_session) beside them.
    A run_task over it waits in Temporal's task queue."""
    return 2 * max(1, cap) + 10


async def start(activities: list, tenants_doc: dict | None = None, *,
                address: str = TEMPORAL_ADDRESS, namespace: str = TEMPORAL_NAMESPACE,
                queue: str = TEMPORAL_TASK_QUEUE, connected: Client | None = None,
                **worker_kwargs) -> bool:
    """True when the worker runs. `connected` skips the connect (tests).
    An address (or a connected client) makes the workflow path the
    configured one, also when the connect fails (then requests answer 503
    until a later start succeeds)."""
    global client, task_queue, _worker, _worker_task, _address
    if connected is None:
        if not address:
            return False
        _address = address
        try:
            connected = await asyncio.wait_for(Client.connect(address, namespace=namespace),
                                               timeout=TEMPORAL_CONNECT_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - any connect failure: not ready (503)
            logger.error("Temporal at %s (%s) unreachable: %r", address, namespace, e)
            return False
    else:
        _address = address or "connected"
    worker_kwargs.setdefault("max_concurrent_activities", max_concurrent_activities())
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


def start_in_background(activities: list, tenants_doc: dict | None = None, *,
                        address: str = TEMPORAL_ADDRESS,
                        reconnect_s: float = TEMPORAL_RECONNECT_S,
                        **kwargs) -> asyncio.Task | None:
    """The supervisor: start(), again every `reconnect_s` while the connect
    fails, and again after the worker exits. None without an address (the
    direct path)."""
    global _start_task, _address
    if not address:
        return None
    _address = address

    async def supervise() -> None:
        while True:
            try:
                started = await start(activities, tenants_doc, address=address, **kwargs)
            except Exception:  # noqa: BLE001 - e.g. build_worker: retried, never fatal
                logger.exception("Temporal worker start failed; retrying in %ss", reconnect_s)
                started = False
            if started:
                task = _worker_task
                if task is not None:
                    await asyncio.wait({task})
            await asyncio.sleep(reconnect_s)

    _start_task = asyncio.get_running_loop().create_task(supervise(), name="temporal-start")
    return _start_task


def _worker_exited(t: asyncio.Task) -> None:
    """A worker that dies on its own (namespace missing, poll failure):
    not ready (requests answer 503) until the supervisor restarted it; the
    direct path is never taken instead. stop() clears _worker_task first,
    so its shutdown is not this."""
    global client, _worker, _worker_task
    if t is not _worker_task:
        return
    e = t.exception() if t.done() and not t.cancelled() else None
    logger.error("Temporal worker exited (%r): not ready until it is restarted", e)
    client, _worker, _worker_task = None, None, None


async def stop() -> None:
    global client, _worker, _worker_task, _start_task, _address
    if _start_task is not None and not _start_task.done():
        _start_task.cancel()
    _start_task = None
    w, t = _worker, _worker_task
    client, _worker, _worker_task = None, None, None
    _address = TEMPORAL_ADDRESS
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
