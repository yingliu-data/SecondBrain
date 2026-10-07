"""Temporal worker bootstrap (PLAN D22): one task queue, ``flows.TASK_QUEUE``.

``start_worker`` is what agent-api calls at startup on its own asyncio loop
(behind ``AGENT_ENGINE=harness``), passing its real activity bodies.  The
``__main__`` below runs the worker standalone with the stub activities, for
the spike and for the compose ``temporal`` service smoke test::

    TEMPORAL_ADDRESS=localhost:7233 uv run python -m flows.worker
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from flows import TASK_QUEUE
from flows import activities as stub_activities
from flows.schedules import SessionSignaller
from flows.session_workflow import WORKFLOWS

DEFAULT_ADDRESS = "localhost:7233"
DEFAULT_NAMESPACE = "secondbrain"


def build_worker(
    client: Client,
    task_queue: str = TASK_QUEUE,
    *,
    activities: Sequence[Callable[..., Any]] | None = None,
    **worker_kwargs: Any,
) -> Worker:
    """A Worker with both workflows, the four session activities and the
    schedule glue activity.  ``activities`` defaults to the stubs in
    ``flows.activities``; agent-api passes its real ``prepare_vm``,
    ``run_task``, ``park_vm`` and ``deliver`` instead (same names, same payloads).
    """
    acts = list(activities if activities is not None else stub_activities.STUB_ACTIVITIES)
    acts.append(SessionSignaller(client).signal_session)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=acts,
        **worker_kwargs,
    )


def start_worker(
    client: Client,
    task_queue: str = TASK_QUEUE,
    *,
    activities: Sequence[Callable[..., Any]] | None = None,
    **worker_kwargs: Any,
) -> tuple[Worker, asyncio.Task[None]]:
    """Build the worker and start polling on the current event loop.

    Returns the ``Worker`` and the task running it; stop both with
    ``await stop_worker(worker, task)``.
    """
    worker = build_worker(client, task_queue, activities=activities, **worker_kwargs)
    task = asyncio.get_running_loop().create_task(
        worker.run(), name=f"temporal-worker:{task_queue}"
    )
    return worker, task


async def stop_worker(worker: Worker, task: asyncio.Task[None]) -> None:
    await worker.shutdown()
    await task


async def connect(
    address: str | None = None, namespace: str | None = None
) -> Client:
    return await Client.connect(
        address or os.environ.get("TEMPORAL_ADDRESS", DEFAULT_ADDRESS),
        namespace=namespace or os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_NAMESPACE),
    )


async def _main() -> None:
    client = await connect()
    worker, task = start_worker(client, os.environ.get("TEMPORAL_TASK_QUEUE", TASK_QUEUE))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    print(f"flows worker on {worker.task_queue} ({client.namespace}); Ctrl-C to stop", flush=True)
    await stop.wait()
    await stop_worker(worker, task)


if __name__ == "__main__":
    asyncio.run(_main())
