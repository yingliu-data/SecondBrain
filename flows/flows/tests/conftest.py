"""Test environment: time-skipping test server, with fallbacks.

Order: ``WorkflowEnvironment.start_time_skipping()`` (downloads the Java
test-server binary once) -> ``start_local()`` (downloads the ``temporal`` CLI
dev server) -> an already running dev server at ``TEMPORAL_TEST_ADDRESS``
(e.g. ``docker run --rm -d -p 7233:7233 temporalio/temporal server start-dev --ip 0.0.0.0``).
Which one was used is printed in the pytest header line ``flows env: ...``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from flows.activities import SessionConfig, SessionRef, TaskSignal
from flows.client import query_status, session_handle, signal_task
from flows.tests import stubs as acts
from flows.tests.stubs import STUB_ACTIVITIES, Script
from flows.worker import build_worker


async def wait_until(pred: Callable[[], Any], timeout: float = 20.0, what: str = "") -> Any:
    """Poll ``pred`` (sync or async) until it returns a truthy value and return it."""
    t0 = time.monotonic()
    while True:
        value = pred()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out waiting for {what or pred}; calls={acts.CALLS}")
        await asyncio.sleep(0.05)


@dataclass
class Env:
    env: WorkflowEnvironment
    kind: str  # "time-skipping" | "local" | "external"

    @property
    def client(self) -> Client:
        return self.env.client

    @property
    def skips_time(self) -> bool:
        return self.kind == "time-skipping"


@pytest_asyncio.fixture(scope="session")
async def tenv() -> AsyncIterator[Env]:
    errors: list[str] = []
    env: WorkflowEnvironment | None = None
    kind = ""
    # FLOWS_TEST_ENV=time-skipping|local|external pins one; default tries all in order.
    wanted = os.environ.get("FLOWS_TEST_ENV", "")
    order = [wanted] if wanted else ["time-skipping", "local", "external"]
    for cand in order:
        try:
            if cand == "time-skipping":
                env = await WorkflowEnvironment.start_time_skipping()
            elif cand == "local":
                env = await WorkflowEnvironment.start_local()
            else:
                addr = os.environ.get("TEMPORAL_TEST_ADDRESS", "localhost:7233")
                env = WorkflowEnvironment.from_client(await Client.connect(addr))
                cand = f"external({addr})"
            kind = cand
            break
        except Exception as e:  # noqa: BLE001 - download/launch/connect failure
            errors.append(f"{cand}: {e!r}")
    if env is None:
        pytest.skip("no Temporal test environment: " + "; ".join(errors))
    print(f"\nflows env: {kind}", flush=True)
    try:
        yield Env(env=env, kind=kind)
    finally:
        if not kind.startswith("external"):
            await env.shutdown()


@dataclass
class Session:
    """One test session: its own task queue, worker and workflow."""

    client: Client
    ref: SessionRef
    task_queue: str
    worker: Worker
    config: SessionConfig
    closed: bool = False

    async def send(self, task: TaskSignal) -> WorkflowHandle:
        return await signal_task(self.client, self.ref, task,
                                 task_queue=self.task_queue, config=self.config)

    def script(self, input_id: str, **kw: Any) -> None:
        """What the ``run_task`` stub does for ``input_id`` (``fail_once``, ``park``, ``outcome``)."""
        acts.SCRIPTS[input_id] = Script(**kw)

    def calls(self, name: str | None = None) -> list[dict]:
        return [c for c in acts.CALLS if name is None or c["activity"] == name]

    async def status(self):
        return await query_status(self.client, self.ref)

    async def wait_for(self, pred: Callable[[], Any], timeout: float = 20.0, what: str = "") -> Any:
        return await wait_until(pred, timeout, what)

    async def wait_state(self, state: str, timeout: float = 20.0):
        """Poll the ``status`` query until ``state`` (idle|running|parked)."""
        async def _in_state():
            st = await self.status()
            return st if st.state == state else None
        return await wait_until(_in_state, timeout, f"state {state}")

    async def wait_delivered(self, n: int, timeout: float = 20.0) -> list[dict]:
        await self.wait_for(lambda: len(self.calls("deliver")) >= n, timeout, f"{n} deliver calls")
        return self.calls("deliver")

    async def close(self) -> None:
        """Terminate the workflow and stop the worker (idempotent)."""
        if self.closed:
            return
        self.closed = True
        try:
            # Let the current activity finish before closing the workflow and
            # the worker: an activity left pending with no worker (or a
            # terminate racing an in-flight activity) stops the Java
            # time-skipping test server from skipping time for every later
            # test (env.sleep fails with "Timeout expired" after 30 s).
            await wait_until(lambda: self._settled(), 10, "task to settle")
            await session_handle(self.client, self.ref).terminate("test done")
        except Exception as e:  # noqa: BLE001 - never started or already closed
            print(f"teardown: {self.ref.session_id}: {e!r}")
        await self.worker.__aexit__(None, None, None)

    async def _settled(self) -> bool:
        """No pending activity and no workflow task in flight, on two polls
        0.25 s apart: an idle run may be mid continue-as-new (history
        trigger), and a terminate racing that hop wedges the time-skipping
        server like a pending activity does.  ``describe`` only, no query: a
        query can time out on the test server right after a hop."""
        for i in range(2):
            if i:
                await asyncio.sleep(0.25)
            raw = (await session_handle(self.client, self.ref).describe()).raw_description
            if raw.pending_activities or raw.HasField("pending_workflow_task"):
                return False
        return True

    async def run_id(self) -> str:
        """Current run id (``describe``; follows continue-as-new)."""
        return (await session_handle(self.client, self.ref).describe()).run_id


@pytest_asyncio.fixture
async def make_session(tenv: Env):
    """Factory: ``await make_session(config=...)`` -> Session with a running worker.

    ``client``, ``ref`` and ``task_queue`` default to the shared test env, a
    fresh random session and a queue of its own.
    """
    opened: list[Session] = []

    async def _make(
        config: SessionConfig | None = None,
        *,
        client: Client | None = None,
        ref: SessionRef | None = None,
        task_queue: str | None = None,
        **worker_kwargs,
    ) -> Session:
        acts.CALLS.clear()
        acts.SCRIPTS.clear()
        client = client or tenv.client
        uid = uuid.uuid4().hex[:8]
        ref = ref or SessionRef(user="u-" + uid, tenant="default", session_id="s-" + uid)
        tq = task_queue or f"test-{uid}"
        worker_kwargs.setdefault("activities", STUB_ACTIVITIES)
        worker = build_worker(client, task_queue=tq, **worker_kwargs)
        await worker.__aenter__()
        s = Session(client=client, ref=ref, task_queue=tq, worker=worker,
                    config=config or SessionConfig())
        opened.append(s)
        return s

    yield _make

    for s in opened:
        await s.close()
