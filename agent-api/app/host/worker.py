"""worker — the host half of one session: Worker (driven directly by act(),
or by the Temporal activities) and Workers, the process's registry.

docs/HOST.md section 1 is the description: the VM lifecycle (boot, park,
wake, idle, death; the VMLifecycle mixin in vm.py), the task API both
drivers share, the cap / wall clock / tickets, what survives an agent-api
restart, idle eviction, and the pump (the single writer of state_sync,
group-committed: an event is acked only once it is fsynced). The hello is
assembled in hello.py, adopted device calls live in adopted.py, task
outcomes and ids in outcomes.py, the settings in settings.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable

from flows.activities import WALL_CLOCK_GRACE_S
from protocol.clock import now_ms
from protocol.fold import TASK_INPUT_KINDS
from protocol.messages import Outbox, ProtocolError, ack, control_message
from protocol.version import HARNESS_VERSION

from app.config import (HARNESS_GUEST_ROOT, HARNESS_MAX_CONCURRENT_TASKS, HARNESS_SANDBOX,
                        HARNESS_SHUTDOWN_PARK_S, HARNESS_WORKER_EVICT_S)
from app.gateway.client import GatewayError, fetch_session_tools
from app.host import hello as hello_mod
from app.host import memory_index
from app.host.adopted import AdoptedCalls
from app.host.admission import Admission
from app.host.bulk_server import BulkServer
from app.host.durable import writer as durable_writer
from app.host.endpoints import ControlChannel, Endpoints
from app.host.gateway_forwarder import GatewayForwarder, GatewayRelay
from app.host.hello import session_config, strip_current_time  # noqa: F401 (re-exported)
from app.host.llm_proxy import LLMProxy
from app.host.outcomes import (  # noqa: F401 (re-exported: routes, activities, tests)
    DIRECT, WORKFLOW, GuestError, answer_input_id, end_error, new_input_id, settle_outcome,
    task_ok, task_outcome, ticket_id_for)
from app.host.sandbox import Sandbox
from app.host.settings import WorkerSettings
from app.host.sides import DEVICE_CALLS, DeviceCalls, Sides
from app.host.state_sync import StateSync, StateSyncError
from app.host.stream import DONE, Projector, fatal_items, sse, tokens
from app.host.vm import VMLifecycle, wake_order, write_json  # noqa: F401 (re-exported)
from app.session.ids import session_key, session_ref

logger = logging.getLogger("host.worker")

# How a control type is handled outside a plain send (every other type is
# held while parked, the guest wakes for it, and carried over a death resume):
DROPPED_WHILE_PARKED = frozenset({"heartbeat", "park"})
# the only controls that wake a guest parked on a question (send_control)
QUESTION_PARK_WAKERS = ("ui_response", "abort")
# a dropped link: how long to wait for the guest to exit (a settled guest
# exits right after settle's ack) or reconnect (a stream drop), whichever
# comes first, before telling a death from a drop
LINK_DROP_WAIT_S = 2.0
# guest events folded and stored per group commit (one write + one fsync)
EVENT_BATCH = 256
# how long close() (and the shutdown) waits for queued log records to be fsynced
CLOSE_FLUSH_TIMEOUT_S = 5.0
# a parked session's resume info, relative to its directory (VMLifecycle)
PARKED_FILE = "vm/parked.json"


def make_sandbox() -> Sandbox:
    if HARNESS_SANDBOX == "firecracker":
        from app.host.sandbox.firecracker import FirecrackerSandbox  # type: ignore
        return FirecrackerSandbox()
    from app.host.sandbox.local import LocalSandbox
    return LocalSandbox(HARNESS_GUEST_ROOT)


async def _first(aw: asyncio.Future | None, evt: asyncio.Event,
                 timeout: float | None = None) -> None:
    """Return when `aw` is done, `evt` is set or `timeout` passed (never cancels `aw`)."""
    waiter = asyncio.ensure_future(evt.wait())
    try:
        await asyncio.wait({waiter} if aw is None else {aw, waiter}, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


class Worker(VMLifecycle):
    def __init__(self, session, tenant, registry, sandbox: Sandbox, sinks=None, *,
                 settings: WorkerSettings | None = None,
                 device_calls: DeviceCalls = DEVICE_CALLS,
                 admission: Admission | None = None,
                 on_event: Callable[[dict], object] | None = None) -> None:
        """Safe off the loop (Workers.open builds it in a thread: the log is
        folded here); on_loop() then finishes what needs the loop.
        `on_event` sees each stored guest event (the terminal chat, app/cli.py)."""
        self.session = session
        self.tenant = tenant
        self.registry = registry
        self.sandbox = sandbox
        self.settings = settings or WorkerSettings.from_config()
        self.sinks: list[asyncio.Queue] = list(sinks or [])
        self._sink_input: dict[int, str] = {}     # id(queue) -> the input it streams
        self.device_calls = device_calls
        self.on_event = on_event
        self.ports = dict(self.settings.ports)

        meta = session.read_meta()
        self.session_id: str = meta.get("session_id") or session.root.name
        self.user: str = tenant.user
        self.ref = session_ref(tenant.user, tenant.name, self.session_id)
        self.session_key = session_key(tenant.user, tenant.name, self.session_id)
        # this session's share of the global task cap (both drivers): held
        # while a task runs, not while it is parked on a question
        self._slot = (admission or Admission(None)).slot()
        self.sync = StateSync(session)
        self.projector = Projector()

        self.endpoints: Endpoints | None = None
        self.proxy: LLMProxy | None = None
        self.sides: Sides | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbound: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None
        self._channel: ControlChannel | None = None   # the link: None = down (_set_channel)
        self._outbox: Outbox | None = None
        self._guest_run = 0          # increments on every sandbox start
        self._stop_accept = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._task_lock = asyncio.Lock()
        # the running task: input_id, its settle, its wall clock (hello limits)
        self._current: tuple[str, asyncio.Future, float] | None = None
        self.last_hello: dict | None = None
        self.closed = False
        self.last_active = time.monotonic()           # Workers' idle eviction
        self.bulk: BulkServer | None = None
        # the :5011 forwarder (per listener) and its worker-lived relay
        # (held calls survive the listener closing around a park)
        self.gateway: GatewayForwarder | None = None
        self.gateway_relay: GatewayRelay | None = None
        # the session's gateway entries from the first successful fetch:
        # pins are fixed per session, a resume hello reuses them
        self.gateway_entries: list[dict] | None = None
        # the live guest's hello limits.answer_wait_s: > 0 = a question
        # deadline parks the task, its driver waits this long for the answer
        # (0 = the guest denies). Decided per guest run by the driver that
        # boots it (answer_wait_for, _boot)
        self.answer_wait_s = self.answer_wait_for(DIRECT)
        # wait primitives: every change below _pulse()s; _until(predicate) waits
        self._changed = asyncio.Event()             # replaced (and set) on every change
        self._control_gen = 0                       # the :5000 listener of this guest run
        self._parked_seen = False                   # the asked-for `parked` was acked
        # none | booting | running | parking | parked; the session snapshot
        # itself is the sandbox's business (has_snapshot / restore)
        self._vm_state = "none"
        # {input_id, question_id}: a field of the parked state (vm_state clears it)
        self._question_park: dict | None = None
        self.snapshot_dir = session.root / "vm" / "snapshot"
        self.parked_file = session.root / PARKED_FILE
        self._vm_lock = asyncio.Lock()
        self._deferred: list[tuple[str, dict]] = []  # controls held while parked
        self._wake_task: asyncio.Task | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._resumes = 0                           # death resumes of the running task
        self._orphan_exit = False                   # _settle_orphan: the guest exits on purpose
        self._side_tasks: dict[str, asyncio.Task] = {}
        self._bg: set[asyncio.Task] = set()         # fire-and-forget tasks (strong refs)
        self._acts: dict[str, asyncio.Task] = {}    # input_id -> its task (start_task)
        self._beats: dict[str, set[Callable[[], None]]] = {}   # input_id -> on_heartbeat
        # the device calls this Worker adopted from a parked session's fold
        self.adopted = AdoptedCalls(self.device_calls, self.ref, send=self._send_adopted,
                                    host_event=self.sync.host_event,
                                    timeout_s=self.settings.device_tool_timeout_s,
                                    closed=lambda: self.closed, label=self.session_key)
        # the driver of the running task (direct | workflow; None: not given)
        self._task_driver: str | None = None
        # a question park's answer deadline (epoch ms; the direct path's
        # answer wait, kept in vm/parked.json across a restart)
        self._answer_deadline_ms: int | None = None
        self._park_driver: str | None = None        # the driver of a restored question park
        # (input_id, task): the direct path's driver of a question park this
        # process restored from vm/parked.json (_drive_restored)
        self._restored: tuple[str, asyncio.Task] | None = None
        self._on_loop_done = False
        self._load_parked()

    def on_loop(self) -> "Worker":
        """The part of a new Worker that needs the running loop (a restarted
        parked session): adopt the device calls its fold has open, and on
        the direct path drive a restored question park. Idempotent."""
        if self._on_loop_done:
            return self
        self._on_loop_done = True
        self._adopt_open_device_calls()
        self._resume_restored_park()
        return self

    def _spawn(self, coro) -> asyncio.Task:
        """A background task the loop would otherwise only hold weakly."""
        task = self._loop.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    # ------------------------------------------------------------ waits
    def _pulse(self) -> None:
        """Something a waiter may wait for changed: wake every _until."""
        self.last_active = time.monotonic()
        evt, self._changed = self._changed, asyncio.Event()
        evt.set()

    async def _until(self, done: Callable[[], bool], aw: asyncio.Future | None = None,
                     timeout: float | None = None) -> float:
        """Wait until `done()` holds (checked on every change, _pulse), `aw`
        (if given) is done or `timeout` (None: no limit) ran out; the time left."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not done() and (aw is None or not aw.done()):
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return 0.0
            await _first(aw, self._changed, timeout=left)
        return float("inf") if deadline is None else max(0.0, deadline - time.monotonic())

    # ------------------------------------------------------------ sinks
    def attach(self, input_id: str | None = None) -> asyncio.Queue:
        """A new sink. With `input_id` it only receives that input's stream
        (tokens, error, done, end-of-stream): two requests can be queued on
        one worker, and the earlier task's end must not close the later
        request's stream (chat.py: a task outlives a client disconnect)."""
        self.last_active = time.monotonic()
        q: asyncio.Queue = asyncio.Queue()
        self.sinks.append(q)
        if input_id is not None:
            self._sink_input[id(q)] = input_id
        for call in self.device_calls.open_for(self.ref):
            # an open device call is re-emitted to the stream that attaches
            q.put_nowait(sse("tool_call", call))
        current = self.current_input_id
        if input_id is None or current is None or input_id == current:
            # so is an open question (the phone gets it on reconnect);
            # not to the stream of an input queued behind the asking task
            for question in self.open_questions():
                q.put_nowait(sse("question", question))
        return q

    def detach(self, q: asyncio.Queue) -> None:
        self.last_active = time.monotonic()
        try:
            self.sinks.remove(q)
        except ValueError:
            pass
        self._sink_input.pop(id(q), None)

    def _emit(self, item, input_id: str | None = None) -> None:
        """Put `item` on every sink of `input_id` (default: the running input;
        no input -> every sink)."""
        if input_id is None and self._current is not None:
            input_id = self._current[0]
        for q in list(self.sinks):
            scope = self._sink_input.get(id(q))
            if scope is None or input_id is None or scope == input_id:
                q.put_nowait(item)

    def _emit_many(self, items, input_id: str | None = None) -> None:
        for s in items:
            self._emit(s, input_id)

    def _end_stream(self, input_id: str | None = None) -> None:
        self._emit(None, input_id)

    def _end_all_streams(self) -> None:
        for q in list(self.sinks):
            q.put_nowait(None)

    # ------------------------------------------------------------ eviction
    def evictable(self) -> bool:
        """Nothing in this process needs the Worker in memory: no task
        (running, queued or kept for a retry), the VM none or parked (its
        snapshot on disk), no adopted device call or open device call, no
        question park (its answer wait / timer), no attached stream, no
        background work. Workers drops it after an idle grace; it is
        rebuilt from disk on demand."""
        return (not self.closed and self._current_future() is None
                and not self._task_lock.locked()
                and all(t.done() for t in self._acts.values())
                and self.vm_state in ("none", "parked")
                and self._question_park is None and self._restored_driver() is None
                and not self.adopted and not self.device_calls.open_for(self.ref)
                and not self.sinks and not self._beats and not self._side_tasks
                and not self._bg
                and (self._wake_task is None or self._wake_task.done()))

    # ------------------------------------------------------------ the task API
    @property
    def current_input_id(self) -> str | None:
        return self._current[0] if self._current_future() is not None else None

    @property
    def task_wall_clock_s(self) -> float:
        """The running task's wall clock, else the session's default."""
        if self._current_future() is not None:
            return self._current[2]
        return self.default_wall_clock_s()

    def default_wall_clock_s(self) -> int:
        """A task's wall clock when its input names none: the tenant's
        `wall_clock_s` (SessionConfig), else the settings' wall_clock_s
        (HARNESS_WALL_CLOCK_S)."""
        return session_config(self.tenant, self.settings.wall_clock_s).wall_clock_s

    def wall_clock_for(self, task_input: dict) -> int:
        """The wall clock of `task_input`'s task: its own `wall_clock_s`
        (the workflow's RunTask), else default_wall_clock_s(). It goes to
        the guest in the `task` message (the guest settles `timeout` at
        it); the host waits it + WALL_CLOCK_GRACE_S (_wait) from when the
        task started (its slot taken), on both paths."""
        return int(task_input.get("wall_clock_s") or self.default_wall_clock_s())

    def open_questions(self) -> list[dict]:
        """Unanswered questions of the current run whose call has no result
        yet, as the SSE `question` payload (text and options from the
        fold's question_text index)."""
        out = []
        state = self.sync.state
        for qid, q in state.open_questions.items():
            call = state.open_calls.get(q["call_id"])
            shown = state.question_text.get(qid)
            if q.get("answered") or call is None or "result" in call or shown is None:
                continue
            out.append({"question_id": qid, "call_id": q["call_id"], "text": shown["text"],
                        "options": shown["options"], "deadline": q["deadline"]})
        return out

    def start_task(self, task_input: dict) -> asyncio.Task:
        """The task of `task_input` (queued behind the running one), owned by
        the worker: the driver that started it may be cancelled or fail, the
        task runs on, and a retry (same input_id) gets the same task back."""
        self.last_active = time.monotonic()
        input_id = task_input["input_id"]
        task = self._acts.get(input_id)
        if task is None or (task.done() and not task_ok(task)):
            task = self._acts[input_id] = asyncio.get_running_loop().create_task(
                self._task(task_input), name=f"act:{self.session_key}:{input_id}")
        return task

    def task_for(self, input_id: str) -> asyncio.Task | None:
        return self._acts.get(input_id)

    def drop_queued(self, input_id: str, reason: str) -> bool:
        """Cancel `input_id`'s task while it has not started: it waits for
        its session's task lock or a slot of the cap (Admission), nothing
        of it went to a guest. True when dropped (host event
        workflow{dropped}); False when there is no such task or it already
        started (then abort_and_wait is the way to end it). run_task calls
        it when the activity is cancelled or times out: the task then never
        runs later, after its workflow gave up on it."""
        task = self._acts.get(input_id)
        if task is None or task.done() or self.current_input_id == input_id:
            return False
        task.cancel()
        # gone: a retry starts it anew; deliver finds no task of it to abort
        del self._acts[input_id]
        self.sync.host_event("workflow", {"kind": "dropped", "input_id": input_id,
                                          "reason": reason})
        return True

    async def wait_outcome(self, input_id: str) -> dict:
        """The settle of `input_id` (its task's result: the settle body, or
        the host's failure result), or {"input_id", "outcome": "parked",
        "question_id"} once the guest parks on a question of that input."""
        task = self._acts.get(input_id)
        if task is None:
            settled = self.sync.settled.get(input_id)
            if settled is not None:
                return settled
            raise GuestError(f"no task for input {input_id}")
        return await self._wait(task, input_id=input_id)

    async def _wait(self, aw: asyncio.Future, *, input_id: str | None = None,
                    wall_clock_s: float | None = None) -> dict:
        """`aw`'s result. With `input_id`: the parked outcome as soon as the
        guest is parked on a question of that input. With `wall_clock_s`:
        asyncio.TimeoutError once it (+ WALL_CLOCK_GRACE_S: the guest
        settles `timeout` at the wall clock itself, this is the backstop)
        ran out; the clock stops while the task is parked on a question
        (the driver's answer wait bounds that) and while the guest is parked
        at all (its own clock is frozen too: an answered question's wake
        waiting for a slot of the task cap is not the task's time)."""
        left = None if wall_clock_s is None else wall_clock_s + WALL_CLOCK_GRACE_S

        def paused() -> bool:
            return self.question_park is not None or self.vm_state == "parked"

        while True:
            if aw.done():               # first: a task can end while still parked
                return aw.result()
            qp = self.question_park
            if input_id is not None and self.parked_on(input_id):
                return {"input_id": input_id, "outcome": "parked",
                        "question_id": qp["question_id"]}
            if paused() or left is None:
                await _first(aw, self._changed)
                continue
            left = await self._until(paused, aw, left)
            if not aw.done() and left <= 0 and not paused():
                raise asyncio.TimeoutError

    def parked_on(self, input_id: str) -> bool:
        """The guest is parked on a question of `input_id`."""
        qp = self.question_park
        return qp is not None and qp["input_id"] == input_id

    def on_heartbeat(self, input_id: str, beat: Callable[[], None]) -> Callable[[], None]:
        """Call `beat()` on every heartbeat tick of `input_id`'s task (from
        its start, also while it is queued); returns the unsubscribe."""
        beats = self._beats.setdefault(input_id, set())
        beats.add(beat)

        def off() -> None:
            beats.discard(beat)
            if not beats and self._beats.get(input_id) is beats:
                del self._beats[input_id]
        return off

    def answer(self, question_id: str, text: str, input_id: str) -> str:
        """ui_response{question_id, answer} to the guest (a parked one is
        restored first, ui_response first) for the task of `input_id` (the
        task that asked); the ui_response's own input_id is
        answer_input_id(input_id, question_id) on both paths, so the guest
        dedupes a re-sent answer. send_control's status, or
        "other_question" (nothing sent) while the guest is parked on another
        question: a ui_response would wake it behind the answer wait."""
        self._ensure_plumbing()
        qp = self.question_park
        if qp is not None and qp["question_id"] != question_id:
            return "other_question"
        self.question_park = None
        ui_input_id = answer_input_id(input_id, question_id)
        status = self.send_control("ui_response", {"input_id": ui_input_id,
                                                   "question_id": question_id, "answer": text})
        self.sync.host_event("workflow", {"kind": "answer_signal", "input_id": input_id,
                                          "answer_input_id": ui_input_id,
                                          "question_id": question_id, "sent": status,
                                          "vm_state": self.vm_state})
        return status

    def abort(self, reason: str) -> str:
        """abort{reason}: the guest settles the running task itself;
        send_control's status."""
        self._ensure_plumbing()
        status = self.send_control("abort", {"reason": reason})
        if status != "no_guest":
            self.sync.host_event("workflow", {"kind": "abort", "reason": reason,
                                              "input_id": self.current_input_id})
        return status

    async def abort_and_wait(self, input_id: str, reason: str, *,
                             question_id: str | None = None, reattach: bool = False) -> None:
        """The one escalation step of both drivers: abort{reason} to the
        guest, then wait (abort_wait_s at most) for that input's task: the
        guest writes the settle itself. `question_id`: the answer wait of that
        question ran out (host event workflow{answer_timeout}). abort{} is
        not input-scoped (the guest settles whatever it runs): only while
        this input's task is the running one. reattach: a parked guest whose
        task this process does not hold (a restarted agent-api) is woken by
        the abort and the task re-attached (the guest dedupes it)."""
        task = self.task_for(input_id)
        orphan = reattach and task is None and self.vm_state == "parked"
        if not orphan and (task is None or task.done() or self.current_input_id != input_id):
            return
        if question_id is not None:
            self.sync.host_event("workflow", {"kind": "answer_timeout", "input_id": input_id,
                                              "question_id": question_id})
        if self.abort(reason) == "no_guest":
            return
        if orphan:
            task = self.start_task({"input_id": input_id, "text": ""})
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.abort_wait_s)
        except Exception as e:  # noqa: BLE001 - timeout or the task's own failure
            logger.warning("session %s: no settle after abort{%s}: %r", self.session_key, reason, e)

    def finish_input(self, input_id: str, error: str | None = None, *,
                     outcome: str | None = None, question_id: str | None = None,
                     driver: str | None = None) -> bool:
        """The one terminal step of an input, on both paths (act() when its
        task returns, the workflow at deliver): its ticket (write_ticket),
        then `error` (if any, with its outcome) + `done` + end of stream to
        its sinks; its task is forgotten. True when this call wrote the
        ticket first (a retried deliver writes it again, the same)."""
        first = False
        try:
            first = self.write_ticket(input_id, outcome=outcome, question_id=question_id,
                                      error=error, driver=driver)
        except Exception:       # the record never keeps the stream open
            logger.exception("session %s: ticket of %s not written", self.session_key, input_id)
        extra = {"outcome": outcome} if outcome is not None else {}
        self._emit_many(fatal_items(error, **extra) if error else [DONE], input_id)
        self._end_stream(input_id)
        self._acts.pop(input_id, None)
        self.last_active = time.monotonic()
        return first

    def write_ticket(self, input_id: str, *, outcome: str | None = None,
                     question_id: str | None = None, error: str | None = None,
                     driver: str | None = None) -> bool:
        """{session}/tickets/{ticket_id}.json, the one record of an input's
        end (ticket_id_for): {ticket_id, input_id, driver, outcome, answer,
        question_id, error, manifest (the settle's, protocol.bulk.manifest),
        usage, completed_at}. The answer, manifest and usage are the
        guest's settle, else the host's failure result. Written once,
        atomically; True when this call wrote it."""
        ticket_id = ticket_id_for(self.ref, input_id)
        path = self.session.tickets / f"{ticket_id}.json"
        if path.exists():
            return False                # written once (a retried deliver, a replay)
        task = self._acts.get(input_id)
        settle = self.sync.settled.get(input_id) or (
            task.result() if task is not None and task_ok(task) else {})
        if outcome is None:
            outcome = settle_outcome(settle.get("outcome"))
        write_json(path, {
            "ticket_id": ticket_id, "input_id": input_id,
            "driver": driver, "outcome": outcome,
            "answer": settle.get("answer", ""), "question_id": question_id,
            "error": error, "manifest": settle.get("manifest", {}),
            "usage": settle.get("usage", {}), "completed_at": now_ms(),
        })
        return True

    def replay(self, input_id: str, sink: asyncio.Queue | None = None) -> dict | None:
        """A settled input's answer again (word-wise), then done + end of
        stream: onto one `sink`, or (none) the input's sinks via finish_input.
        Its settle body; None when the input has not settled."""
        body = self.sync.settled.get(input_id)
        if body is None:
            return None
        items = tokens(body["answer"].strip()) if body.get("answer") else []
        if sink is None:
            self._emit_many(items, input_id)
            self.finish_input(input_id)
        else:
            for item in (*items, DONE, None):
                sink.put_nowait(item)
        return body

    # ------------------------------------------------------------ hello (hello.py)
    def tool_entries(self) -> list[dict]:
        """hello.config.tools: host/device tools, then the gateway's mcp
        entries (load_gateway_entries) except one named like a host tool."""
        return hello_mod.merge_tool_entries(self.host_tool_entries(), self.gateway_entries,
                                            self.session_key)

    def host_tool_entries(self) -> list[dict]:
        return hello_mod.host_tool_entries(self.tenant, self.registry)

    async def load_gateway_entries(self) -> list[dict]:
        """GET /internal/tools for this session (async httpx, short timeout).
        The first successful answer is kept for the session: the gateway
        pins it, and a resume hello reuses it even when the gateway is down
        then. A failure is logged and the session starts without MCP tools
        (the next boot tries again)."""
        if self.gateway_entries is not None or not self.settings.gateway_url:
            return self.gateway_entries or []
        try:
            entries = await fetch_session_tools(self.user, self.tenant.name, self.session_id,
                                                base_url=self.settings.gateway_url,
                                                timeout_s=self.settings.gateway_tools_timeout_s)
        except GatewayError as e:
            logger.warning("session %s: starting without MCP tools (%s)", self.session_key, e)
            return []
        self.gateway_entries = entries
        return entries

    def system_block(self) -> dict:
        return hello_mod.system_block(self.tenant, self.session, self.user,
                                      self.settings.system_prompt)

    def attachments(self, text: str) -> list[dict]:
        """The memory files recalled by description, then the memory graph's nearest nodes."""
        recalled = hello_mod.attachments(self.tenant, self.session, self.user, text)
        return recalled + memory_index.recall(self.user, self.tenant.name, self.session_id, text,
                                              [item["slug"] for item in recalled])

    def answer_wait_for(self, driver: str | None) -> int:
        return hello_mod.answer_wait_for(self.tenant, driver)

    def build_hello(self, resume: bool = False) -> dict:
        return hello_mod.build_hello(self.settings, self.tenant, session_id=self.session_id,
                                     tools=self.tool_entries(), system=self.system_block(),
                                     wall_clock_s=self.default_wall_clock_s(),
                                     answer_wait_s=self.answer_wait_s, resume=resume)

    # ------------------------------------------------------------ control
    def _lost(self, call_id) -> bool:
        """The current run's fold has `call_id` ended op_state{lost} (a
        resumed guest gave up on it): also after an agent-api restart."""
        call = self.sync.state.open_calls.get(call_id)
        return call is not None and call["state"] == "lost"

    def send_control(self, type: str, body: dict) -> str:
        """The one way a control reaches the guest. Returns
          "sent"      numbered (next cseq) and kept until acked; sent now if
                      the link is up, else with the resend on the next link
                      (a stream drop between restore and reconnect is not a
                      lost guest)
          "held"      the guest is parked: kept unnumbered, the guest wakes
                      (restore) and the wake numbers the held controls
                      `ui_response` first. Parked on a question, only
                      `ui_response` and `abort` wake it (the driver's answer
                      wait holds the task; a woken guest would not park
                      again): the others wait for that wake
          "dropped"   not sent at all: a `side_result` for a call the guest
                      declared lost (the fold's op_state; logged
                      `input{dedupe: late}`), or a heartbeat / park to a
                      parked guest
          "no_guest"  no guest run can take it (never booted, or gone)"""
        if type == "side_result" and self._lost(body.get("call_id")):
            self.sync.host_event("input", {"kind": "side_result", "call_id": body.get("call_id"),
                                           "side": body.get("side"), "dedupe": "late"})
            return "dropped"
        if self.vm_state == "parked":
            if type in DROPPED_WHILE_PARKED:
                return "dropped"
            self._deferred.append((type, body))
            if self.question_park is None or type in QUESTION_PARK_WAKERS:
                self._request_wake()
            return "held"
        if self._outbox is None or self.vm_state == "none":
            return "no_guest"
        msg = control_message(self.session_id, self._outbox.next_number(), type, body)
        self._outbox.push(msg)
        self._send_raw(msg)
        return "sent"

    def _send_raw(self, msg: dict) -> None:
        if self._channel is not None and not self._channel.closed:
            self._channel.send(msg)

    def _send_held(self, items: list[tuple[str, dict]]) -> None:
        """Number and send held controls, `ui_response` first (wake_order)."""
        for t, b in sorted(items, key=wake_order):
            self.send_control(t, b)

    # ------------------------------------------------------------ plumbing
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._inbound = asyncio.Queue()
        return loop

    def _ensure_plumbing(self) -> None:
        """The session's listeners, LLM proxy, accept thread, Sides and the
        pump: made on the first use that needs a guest (a task makes them
        only once it holds its slot of the cap)."""
        loop = self._ensure_loop()
        if self.endpoints is None:
            if self.settings.gateway_url and "gateway" in self.ports:
                self.gateway_relay = GatewayRelay(self.settings.gateway_url, user=self.user,
                                                  tenant=self.tenant.name,
                                                  session=self.session_id,
                                                  host_event=self.sync.host_event)
            # no gateway: no :5011 listener at all (the guest has no mcp tools)
            listen = (self.ports if self.gateway_relay is not None
                      else {k: v for k, v in self.ports.items() if k != "gateway"})
            self.endpoints = Endpoints(self.session.root, listen)
            self.proxy = LLMProxy(self.endpoints.listener(self.ports["llm"]),
                                  self.settings.llm_url, self.sync.host_event,
                                  timeout_s=self.settings.llm_timeout_s,
                                  token_budget=self.settings.token_budget,
                                  vendor=self.settings.llm_vendor,
                                  api_key_env=self.settings.llm_api_key_env)
            self.proxy.start()
            if self.vm_state == "parked":
                self._close_guest_ports()       # a restarted worker: listen at the wake
            else:
                self._open_guest_ports()
            self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True,
                                                   name=f"accept:{self.session_key}")
            self._accept_thread.start()
        if self.sides is None:
            self.sides = Sides(self.registry, self.tenant, self.session, loop,
                               send_control=self.send_control, host_event=self.sync.host_event,
                               device_calls=self.device_calls,
                               note_host_result=self.projector.note_host_result,
                               session_ref=self.ref,
                               host_timeout_s=self.settings.host_tool_timeout_s,
                               device_timeout_s=self.settings.device_tool_timeout_s)
        if self._pump is None or self._pump.done():
            self._pump = loop.create_task(self._pump_loop(), name=f"pump:{self.session_key}")

    def _on_bulk_put(self, kind: str, path: str, nbytes: int, sha256: str) -> None:
        self.sync.host_event("bulk", {"kind": kind, "path": path, "bytes": nbytes, "sha256": sha256})

    def _accept_loop(self) -> None:
        """Blocking accepts on :5000 (no polling: while the guest is parked
        the port is closed and accept waits for reopen). close() sets
        _stop_accept before Endpoints.close(), which ends the wait (None)."""
        n = 0
        while not self._stop_accept.is_set():
            endpoints = self.endpoints
            if endpoints is None:
                return
            got = endpoints.accept_control_gen(timeout=None)
            if got is None:
                continue                # closed: the loop condition ends it
            conn, gen = got
            n += 1
            ch = ControlChannel(conn, self._loop, self._inbound, name=f":{n}", generation=gen,
                                start=False)
            # `link up` queued before the reader runs: the guest's hello (sent
            # at connect) and a quick link down always come after it, never a
            # down that is ignored and then an up of a dead channel
            try:
                self._loop.call_soon_threadsafe(self._inbound.put_nowait, ("link", "up", ch))
            except RuntimeError:
                ch.close()              # the loop is gone
                return
            ch.start()

    # ------------------------------------------------------------ restart
    def _resume_restored_park(self) -> None:
        """A question park of the direct path restored from vm/parked.json
        (an agent-api restart): no act() holds its task, so this process
        drives it (_drive_restored): the answer wait goes on with what is
        left of it. The workflow path's parks are its timer's business."""
        qp = self._question_park
        if qp is None or self._park_driver != DIRECT:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("session %s: question park restored without a loop: nothing "
                           "drives its answer wait", self.session_key)
            return
        task = loop.create_task(self._drive_restored(qp["input_id"]),
                                name=f"restored:{self.session_key}:{qp['input_id']}")
        self._restored = (qp["input_id"], task)
        self.sync.host_event("workflow", {"kind": "question_park_restored", **qp,
                                          "answer_left_s": round(self.answer_left_s(), 3)})

    def answer_left_s(self) -> float:
        """What the answer wait of the current question park has left."""
        if self._answer_deadline_ms is None:
            return float(self.answer_wait_s)
        return max(0.0, (self._answer_deadline_ms - now_ms()) / 1000)

    async def _drive_restored(self, input_id: str) -> dict | None:
        """The direct path's driver of a restored question park: wait for
        the answer (POST /chat answer_to -> answer(): the guest wakes, the
        task is re-attached here and driven as act() would, _drive) or the
        rest of the answer wait (abort{escalated}: the guest settles it).
        The task's streams end at its end (finish_input). A new input of
        the session waits for this one (_act)."""
        park = self.question_park
        await self._until(lambda: self.question_park != park, timeout=self.answer_left_s())
        if self.closed:
            return None
        if self.question_park == park:
            await self.abort_and_wait(input_id, "escalated", question_id=park["question_id"],
                                      reattach=True)
            task = self.task_for(input_id)
            if task is None:
                return None
        else:
            task = self.start_task({"input_id": input_id, "text": "", "driver": DIRECT})
        self._finish_on_done(input_id, task)
        return await self._drive(input_id, task)

    def _adopt_open_device_calls(self) -> None:
        """A new Worker on a parked session (an agent-api restart): the
        device calls the fold still has open are adopted (adopted.py), so
        a POST /tool-result after the restart is accepted and a
        re-attaching stream re-emits them. Only for a parked run: the guest
        then resumes from that park (session snapshot, or base restore + a
        log return ending with `parked`) and its side calls wait again; a
        guest that died mid-run marks them lost on its resume (409)."""
        if self.vm_state != "parked" or not self.sync.state.parked:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self.adopted.adopt(self.sync.state, loop)

    def _send_adopted(self, type: str, body: dict) -> str:
        """An adopted call's result: plumbing first (a restarted Worker may
        have none yet), then send_control."""
        self._ensure_plumbing()
        return self.send_control(type, body)

    # ------------------------------------------------------------ act
    async def act(self, task_input: dict) -> dict:
        """The direct path: an in-process driver over the task API (the
        workflow's activities are the other one): start_task, its result
        (a question park waits for the answer, _drive), finish_input. A
        redelivered input (an input_id already seen) is sent again anyway -
        the guest dedupes - and shares the running task; one that already
        settled gets its answer replayed, no guest."""
        input_id = task_input["input_id"]
        task_input = {**task_input, "driver": DIRECT}
        task = self._acts.get(input_id)
        if task is not None and not task.done():
            if self.current_input_id == input_id and (self.vm_state == "parked"
                                                      or self._guest_ready()):
                task_input = await self._with_attachments(task_input)
                self.send_control(*self._task_message(task_input))
            return await asyncio.shield(task)
        restored = self._restored_driver(input_id)
        if restored is not None:
            # parked on a question of this input since before a restart: the
            # restored driver holds it (a start here would wake the guest
            # behind the answer wait); its end ends this request's stream too
            res = await asyncio.shield(restored)
            return res if res is not None else self._fail(input_id, "failed",
                                                          "The parked task could not be resumed.")
        body = self.replay(input_id)      # settled: its answer again, its streams end
        if body is not None:
            return self._settled_again(input_id, body)
        task = self.start_task(task_input)
        self._finish_on_done(input_id, task)
        return await self._drive(input_id, task)

    def _finish_on_done(self, input_id: str, task: asyncio.Task) -> None:
        """The direct path's finish_input of `input_id` when `task` ends;
        added before the waiters' callbacks: the streams end before act()
        returns."""
        def _finish(t: asyncio.Task) -> None:
            outcome = task_outcome(t)
            self.finish_input(input_id, end_error(t, outcome), outcome=outcome, driver=DIRECT)
        task.add_done_callback(_finish)

    def _restored_driver(self, input_id: str | None = None) -> asyncio.Task | None:
        """The running driver of a restored question park (of `input_id`, if
        given), else None."""
        if self._restored is None or self._restored[1].done():
            return None
        if input_id is not None and self._restored[0] != input_id:
            return None
        return self._restored[1]

    async def _drive(self, input_id: str, task: asyncio.Task) -> dict:
        """`task`'s result, as the session workflow drives it: at a
        question park the stream stays open (the question went out) and the
        answer is awaited up to answer_wait_s (POST /chat answer_to ->
        answer() wakes the guest, the task runs on); none in time -> abort
        {escalated}: the guest is woken and writes the escalated settle."""
        while True:
            res = await self._wait(task, input_id=input_id)
            if res.get("outcome") != "parked":
                return res
            park = self.question_park
            await self._until(lambda: self.question_park != park, task, self.answer_left_s())
            if self.question_park == park and not task.done():
                await self.abort_and_wait(input_id, "escalated",
                                          question_id=res.get("question_id"))
                if task.done():
                    return task.result()
                return self._fail(input_id, "escalated",
                                  f"No settle within {self.settings.abort_wait_s:g}s "
                                  f"of abort{{escalated}}.")

    async def _task(self, task_input: dict) -> dict:
        """start_task's task: _act, with the task's heartbeat beside it."""
        hb = asyncio.get_running_loop().create_task(self._heartbeat(task_input["input_id"]))
        try:
            return await self._act(task_input)
        finally:
            hb.cancel()

    async def _act(self, task_input: dict) -> dict:
        """Run one input as a task: its settle body, or the host's failure
        result (_fail). The streams are left open (finish_input)."""
        if self.closed:
            raise GuestError("worker closed")
        input_id = task_input["input_id"]
        restored = self._restored_driver()
        if restored is not None and self._restored[0] != input_id:
            # a question park restored after a restart holds the session
            # (as act()'s session lock did before it): wait for its task
            await asyncio.gather(asyncio.shield(restored), return_exceptions=True)
        self._ensure_loop()
        async with self._task_lock:     # first: queued behind its session, no slot held
            body = self.sync.settled.get(input_id)
            if body is not None:
                return self._settled_again(input_id, body)
            try:
                # the global cap (Workers' Admission), before anything is
                # sent; given back at a question park (_park_locked), taken
                # again by the wake that resumes it (_slot_for_wake)
                await self._slot.acquire()
                try:
                    # the listeners, proxy and threads only once a slot is held
                    self._ensure_plumbing()
                except Exception as e:      # e.g. the unix socket path is too long
                    logger.exception("session %s: host plumbing failed", self.session_key)
                    message = f"The agent sandbox failed: {e}"
                    self._fail(input_id, "failed", message)
                    raise GuestError(message) from e
                fut = self._loop.create_future()
                self._current = (input_id, fut, self.wall_clock_for(task_input))
                self._task_driver = task_input.get("driver")
                try:
                    result = await self._run(task_input, fut)
                except BaseException as e:
                    if not fut.done():
                        fut.set_exception(e)
                    raise
                if not fut.done():
                    fut.set_result(result)
                return result
            finally:
                self._slot.release()

    async def _with_attachments(self, task_input: dict) -> dict:
        """`task_input` with its attachments: the memory recall when it names
        none (it reads files: off the loop)."""
        if task_input.get("attachments") is not None:
            return task_input
        return {**task_input, "attachments": await asyncio.to_thread(
            self.attachments, task_input.get("text", ""))}

    def _task_message(self, task_input: dict) -> tuple[str, dict]:
        """The `task` control of `task_input` (_with_attachments first)."""
        kind = task_input.get("kind", "task")
        if kind not in TASK_INPUT_KINDS:
            kind = "task"
        return kind, {"input_id": task_input["input_id"], "text": task_input.get("text", ""),
                      "attachments": task_input.get("attachments") or [],
                      "wall_clock_s": self.wall_clock_for(task_input)}

    def _settled_again(self, input_id: str, body: dict) -> dict:
        """`body`, the stored settle of an input seen again (logged)."""
        self.sync.host_event("workflow", {"kind": "task_signal", "input_id": input_id,
                                          "dedupe": "settled"})
        return body

    async def _run(self, task_input: dict, fut: asyncio.Future) -> dict:
        input_id = task_input["input_id"]
        wall_clock_s = self.task_wall_clock_s
        self.sync.host_event("workflow", {"kind": "task_signal", "input_id": input_id})
        self._cancel_idle()
        self._resumes = 0
        guest_settled = False
        try:
            await self._settle_orphan(input_id, task_input.get("driver"))
            await self.ensure_ready(input_id, task_input.get("driver"))
            task_input = await self._with_attachments(task_input)
            self.projector.begin_task()         # before any delta can arrive
            self.send_control(*self._task_message(task_input))
            try:
                result = await self._wait(fut, wall_clock_s=wall_clock_s)
                guest_settled = True
            except asyncio.TimeoutError:
                result = self._fail(input_id, "timeout",
                                    f"The task exceeded its wall clock of {int(wall_clock_s)}s.")
        except GuestError as e:
            result = self._fail(input_id, "failed", f"The agent sandbox failed: {e}")
        except Exception as e:
            logger.exception("task %s failed", input_id)
            result = self._fail(input_id, "failed", f"The agent failed: {e}")
        self.sync.host_event("workflow", {"kind": "completed", "input_id": input_id,
                                          "outcome": result.get("outcome")})
        async with self._vm_lock:
            if self.vm_state != "parked" and (not guest_settled
                                              or not self.sandbox.is_alive()):
                # a guest that did not settle may still be working: never keep it
                await self._destroy("settle" if result.get("outcome") == "done"
                                    else result.get("outcome", "failed"))
        if self.sides is not None:
            await self.sides.cancel_all()
        if self.vm_state == "running" and not self.closed:
            self._arm_idle()
        return result

    async def _settle_orphan(self, input_id: str, driver: str | None) -> None:
        """Before `input_id` runs on a guest that holds another input's
        unsettled task no driver in this process waits for (its process
        went away while the guest was parked, or its driver gave up while
        the guest stayed parked): abort{aborted} so the guest settles it
        first (the new task would otherwise only be staged into it). A guest
        exits after an abort's settle: it is stopped and the run resumed
        (hello{resume: true}, the log return ends with that settle), so the
        new task continues the same conversation. No settle within
        abort_wait_s: the guest is destroyed and the next boot opens a new
        run. Without a guest the boot opens a new run (ensure_ready resumes
        only the task's own run)."""
        orphan = self.sync.open_input
        if orphan is None or orphan == input_id or self.vm_state not in ("parked", "running"):
            return
        # the guest exits after the abort's settle: not a death of this task
        # (_link_down), until the guest run below replaces it
        self._orphan_exit = True
        try:
            status = self.send_control("abort", {"reason": "aborted"})
            self.sync.host_event("workflow", {"kind": "orphan_abort", "input_id": orphan,
                                              "before": input_id, "sent": status})
            if status == "no_guest":
                return
            await self._until(lambda: orphan in self.sync.settled,
                              timeout=self.settings.abort_wait_s)
            async with self._vm_lock:
                await self._destroy("orphan")
                self._clear_parked_file()
                self.vm_state = "none"          # not the parked guest that holds it again
                if orphan in self.sync.settled:
                    await self._boot(resume=True, driver=driver)
                else:
                    logger.warning("session %s: orphan task %s did not settle after abort",
                                   self.session_key, orphan)
        finally:
            self._orphan_exit = False

    def _fail(self, input_id: str, outcome: str, message: str) -> dict:
        """The host's failure result of a task: its message as the answer
        (streamed) and `host_error` (finish_input's error)."""
        logger.error("task %s: %s (%s)", input_id, message, outcome)
        self._emit_many(tokens(message), input_id)
        return {"input_id": input_id, "outcome": outcome, "answer": message,
                "usage": {}, "manifest": {}, "host_error": message}

    async def _heartbeat(self, input_id: str) -> None:
        """Every heartbeat_s while the task of `input_id` lives: its
        on_heartbeat listeners (the run_task activity's Temporal heartbeat,
        also while the task is queued); while it runs, the guest heartbeat
        (a parked guest gets none, the host event says parked; none at all
        while parked on a question: its driver's answer wait holds it)."""
        while True:
            await asyncio.sleep(self.settings.heartbeat_s)
            for beat in list(self._beats.get(input_id, ())):
                try:
                    beat()
                except Exception:
                    logger.exception("session %s: heartbeat listener failed", self.session_key)
            if self.current_input_id != input_id or self.question_park is not None:
                continue
            if self.vm_state in ("parked", "parking"):
                self.sync.host_event("workflow", {"kind": "heartbeat", "at": now_ms(),
                                                  "acked_seq": self.sync.last_seq,
                                                  "parked": True})
            elif self._guest_ready():
                self.send_control("heartbeat", {"at": now_ms()})
                self.sync.host_event("workflow", {"kind": "heartbeat", "at": now_ms(),
                                                  "acked_seq": self.sync.last_seq})

    # ------------------------------------------------------------ pump
    def _current_future(self) -> asyncio.Future | None:
        if self._current is not None and not self._current[1].done():
            return self._current[1]
        return None

    async def _pump_loop(self) -> None:
        """The single consumer of the inbound queue. A run of guest events
        already queued is taken together (up to EVENT_BATCH) and stored as
        one group commit (_on_events)."""
        held = None
        while True:
            item = held if held is not None else await self._inbound.get()
            held = None
            try:
                if item[0] == "event":
                    batch = [item[1]]
                    while len(batch) < EVENT_BATCH:
                        try:
                            nxt = self._inbound.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if nxt[0] != "event":
                            held = nxt
                            break
                        batch.append(nxt[1])
                    await self._on_events(batch)
                else:
                    await self._dispatch(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("pump: %s", e)
                fut = self._current_future()
                if fut is not None:
                    fut.set_exception(GuestError(str(e)))

    async def _dispatch(self, item) -> None:
        kind = item[0]
        if kind == "link":
            await self._on_link(item[1], item[2])
        elif kind == "live":
            self._on_live(item[1])
        elif kind == "ack":
            msg = item[1]
            if "ack_cseq" in msg and self._outbox is not None:
                try:
                    self._outbox.acknowledge(msg["ack_cseq"])
                except ProtocolError as e:
                    logger.warning("guest ack rejected: %s", e)
        elif kind == "control":
            logger.warning("guest sent a control message %r; ignored", item[1].get("type"))

    async def _on_link(self, what: str, ch: ControlChannel) -> None:
        if what == "up":
            if ch.generation is not None and ch.generation != self._control_gen:
                # accepted on an earlier :5000 listener: a guest run that was
                # stopped meanwhile (_start_guest listens anew), not this one
                ch.close()
                return
            if self._channel is not None and self._channel is not ch:
                self._channel.close()
            self._set_channel(ch)
            # the host answers with its ack of seq, then resends
            # everything the guest has not acked (its handshake ack arrives as a
            # normal ack; the guest's inbox drops what it already delivered).
            ch.send(ack(self.session_id, ack_seq=self.sync.last_seq))
            if self._outbox is not None:
                for msg in self._outbox.pending:
                    ch.send(msg)
            return
        if ch is not self._channel:
            return
        self._set_channel(None)
        if self.vm_state == "parked" or (self.vm_state == "parking" and self._parked_seen):
            return                      # the parked guest closed its sockets on purpose
        # off the pump: a reconnect (link up) must get through meanwhile
        self._spawn(self._link_down(self._current_future(), self._guest_run))

    async def _link_down(self, fut: asyncio.Future | None, run: int) -> None:
        """The control link of guest run `run` dropped. A settled guest
        exits right after settle's ack; a dropped stream reconnects: wait
        for whichever comes first (at most LINK_DROP_WAIT_S), then tell a
        stream drop, an exit at settle and a death mid-task apart."""
        exited = asyncio.ensure_future(asyncio.to_thread(self.sandbox.wait_exit,
                                                         LINK_DROP_WAIT_S))
        try:
            await self._until(lambda: self._channel is not None or self._guest_run != run,
                              exited, LINK_DROP_WAIT_S)
        finally:
            if not exited.done():
                exited.cancel()
        if self._guest_run != run or self.closed:
            return                      # a new guest run began meanwhile
        if self._orphan_exit:
            return                      # exit after an orphan abort: _settle_orphan boots the next
        if self.sandbox.is_alive() or self._channel is not None:
            return                      # a stream drop: the guest reconnects
        if self.vm_state in ("none", "booting", "parked"):
            return                      # stopped on purpose, or a new guest is starting
        if fut is None or self._current_future() is not fut:
            if self.vm_state == "running":
                self.vm_state = "none"  # an idle guest went away (exit at settle)
            return
        # death mid-task
        if self.vm_state == "parking":
            self.vm_state = "running"   # the waiting park() gives up at once
        if self._resumes >= self.settings.max_resumes:
            msg = ("the guest died again after a resume" if self._resumes
                   else "the guest exited before settling the task")
            fut.set_exception(GuestError(msg))
            return
        self._resumes += 1
        self._spawn(self._death_resume(fut, run))

    async def _death_resume(self, fut: asyncio.Future, run: int) -> None:
        """Restore the base snapshot on the same workspace, hello{resume:
        true}; the guest pulls the log return on :5001 and marks open calls
        lost. max_resumes per task."""
        self.sync.host_event("workflow", {"kind": "resume", "reason": "guest_died",
                                          "resumes": self._resumes,
                                          "acked_seq": self.sync.last_seq})
        try:
            async with self._vm_lock:
                if self._current_future() is not fut or self._guest_run != run:
                    return              # settled meanwhile, or a new guest run already began
                await self._boot(resume=True)
        except Exception as e:
            logger.exception("session %s: resume after death failed", self.session_key)
            if not fut.done():
                fut.set_exception(GuestError(f"resume after the guest died failed: {e}"))

    async def _on_events(self, events: list[dict]) -> None:
        """A run of guest events: each validated and folded, its append
        queued (StateSync.submit); then one wait for the group commit, ONE
        ack of the highest durable seq, and only then each stored event's
        effects in order (stream, side calls, settle). Nothing is acked or
        acted on before it is on disk. A refused event (foreign session,
        schema or fold error) ends the run: the ones before it still count,
        then the error goes to the running task (the pump)."""
        stored: list[tuple[dict, object]] = []
        error: Exception | None = None
        for ev in events:
            if ev.get("session_id") != self.session_id:
                error = StateSyncError(f"event for foreign session {ev.get('session_id')!r}")
                break
            self._before_ack(ev)
            try:
                fut = self.sync.submit(ev)
            except StateSyncError as e:
                error = e
                break
            if fut is not None:
                stored.append((ev, fut))
        if stored:
            try:
                await asyncio.wrap_future(stored[-1][1])
            except Exception:  # noqa: BLE001 - per event below
                pass
        durable = []
        for ev, fut in stored:
            if not fut.done() or fut.exception() is not None:
                error = error or StateSyncError(
                    f"seq {ev['seq']}: not stored: {fut.exception() if fut.done() else 'pending'}")
                break
            self.sync.stored(fut)
            durable.append(ev)
        # durable = host ack after append; a refused event acks nothing new
        self._send_raw(ack(self.session_id, ack_seq=self.sync.last_seq))
        for ev in durable:
            await self._after_event(ev)
        if error is not None:
            raise error

    def _before_ack(self, ev: dict) -> None:
        """What a guest event sets off before it is submitted (so before its
        ack; not yet validated): a `parked` that is next in seq closes the
        guest ports - once it is acked the guest closes and idle-waits, and
        must find no listener (sandbox finding)."""
        if ev.get("type") == "parked" and ev.get("seq") == self.sync.state.last_seq + 1:
            self._close_guest_ports()

    async def _after_event(self, ev: dict) -> None:
        """What a stored (acked) guest event sets off in the host, in order:
        a `memory` event's projection (off the loop) first."""
        if ev["type"] == "memory":
            await asyncio.to_thread(self.sync.project, ev)
        self.last_active = time.monotonic()
        self._emit_many(self.projector.project(ev))
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:   # the hook never breaks the engine
                logger.exception("session %s: on_event failed", self.session_key)
        etype, body = ev["type"], ev["body"]
        if etype == "session_start":
            self._check_harness_version(body.get("harness_version"))
        if etype == "tool_call" and self.sides is not None:
            task = self.sides.handle_tool_call(ev)
            if task is not None:
                cid = body["call_id"]
                self._side_tasks[cid] = task
                task.add_done_callback(lambda _t, cid=cid: self._side_tasks.pop(cid, None))
        elif etype == "op_state" and body["state"] == "lost":
            self._on_lost(body["call_id"])
        elif etype == "tool_result":
            # terminal (also when the guest timed it out): a late POST
            # /tool-result is a 409, and a device call still waiting stops
            cid = body["call_id"]
            if self.device_calls.is_open(cid):
                task = self._side_tasks.pop(cid, None)
                if task is not None and not task.done():
                    task.cancel()
            self.device_calls.discard(cid)
        elif etype == "parked":
            self._set_parked_seen(True)
            if self.vm_state == "running":      # not asked for: the guest parked itself
                self.vm_state = "parking"
                self._spawn(self._adopt_park(body["reason"], body.get("question_id"),
                                             self.current_input_id))
        elif etype == "settle":
            fut = self._current_future()
            if fut is not None and body["input_id"] == self._current[0]:
                fut.set_result(body)
            elif fut is None and self.vm_state == "running" and not self.closed:
                self._arm_idle()        # a task no driver held (woken by a side result)
            self._pulse()
        elif etype == "error" and body["class"] == "fatal":
            fut = self._current_future()
            if fut is not None:
                fut.set_exception(GuestError(body["message"]))

    def _check_harness_version(self, guest_version) -> None:
        """Warn (host event `harness_skew`, never fatal) when the guest's
        session_start names another harness version than this host's."""
        if guest_version == HARNESS_VERSION:
            return
        logger.warning("session %s: guest harness_version %r != host %r", self.session_key,
                       guest_version, HARNESS_VERSION)
        self.sync.host_event("harness_skew", {"guest": guest_version, "host": HARNESS_VERSION})

    def _on_lost(self, call_id: str) -> None:
        """The resumed guest gave up on a host-originated call: stop running
        it; a result that still turns up is dropped (send_control reads the
        lost state from the fold)."""
        if self.gateway_relay is not None:
            self.gateway_relay.discard(call_id)     # an mcp call: drop its held answer
        task = self._side_tasks.pop(call_id, None)
        if task is not None and not task.done():
            task.cancel()
        was_device = self.device_calls.is_open(call_id)
        self.device_calls.discard(call_id)     # a late POST /tool-result is a 409
        if task is not None or was_device:
            self.sync.host_event("side_call", {"call_id": call_id, "state": "lost",
                                               "cancelled": True})

    def _on_live(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "log":
            logger.info("guest: %s", msg.get("body"))
            return
        if t == "ready":
            return
        self._emit_many(self.projector.live(msg))

    # ------------------------------------------------------------ close
    async def close(self, reason: str = "close") -> None:
        if self.closed:
            return
        self.closed = True
        self._cancel_idle()
        self._slot.release()
        self._stop_accept.set()
        fut = self._current_future()
        if fut is not None:
            fut.set_exception(GuestError("worker closed"))
        if self._wake_task is not None and not self._wake_task.done():
            self._wake_task.cancel()
        if self._restored is not None and not self._restored[1].done():
            self._restored[1].cancel()
        if self.sides is not None:
            await self.sides.cancel_all()
        self.adopted.release_all()
        try:
            await self._destroy(reason)
        except Exception:
            pass
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):
                pass
        # the proxy, bulk and gateway servers stop independently: side by side
        await asyncio.gather(*(asyncio.to_thread(server.stop)
                               for server in (self.proxy, self.bulk, self.gateway)
                               if server is not None))
        if self.gateway_relay is not None:
            self.gateway_relay.close()
        if self.endpoints is not None:
            self.endpoints.close()
        if self._accept_thread is not None:
            await asyncio.to_thread(self._accept_thread.join, 2)
        self._end_all_streams()
        # the host records written fire-and-forget (the vm destroy above,
        # ...) are on disk once close() returns: an evicted or shut down
        # session's log is complete, not a segment the writer still holds
        # (bounded: a stuck disk must not hang an eviction or the shutdown)
        await asyncio.to_thread(self.sync.flush_host, CLOSE_FLUSH_TIMEOUT_S)


class Workers:
    """One Worker per session in memory, by session_key(user, tenant,
    session_id); an idle one is evicted (Worker.evictable, idle for
    `evict_s`) and rebuilt from disk on demand.

    `sessions` (the DirStore) opens a new Worker's session directory; every
    new Worker gets `settings` (its tenant's config it reads itself), the
    dependency kwargs (device_calls) and a slot of `admission`, the global
    cap on running tasks (max_concurrent, HARNESS_MAX_CONCURRENT_TASKS;
    None = no cap) that both drivers go through (Worker._act)."""

    def __init__(self, sandbox_factory=make_sandbox, *, sessions=None,
                 max_concurrent: int | None = HARNESS_MAX_CONCURRENT_TASKS,
                 settings: WorkerSettings | None = None,
                 evict_s: float = HARNESS_WORKER_EVICT_S,
                 **worker_kwargs) -> None:
        self._workers: dict[str, Worker] = {}
        self._opening: dict[str, asyncio.Future] = {}
        self.sandbox_factory = sandbox_factory
        self.sessions = sessions
        self.admission = Admission(max_concurrent)
        self.settings = settings or WorkerSettings.from_config()
        self.evict_s = evict_s
        self.worker_kwargs = worker_kwargs
        self._reaper: asyncio.Task | None = None
        self.on_closed: Callable[[str], object] | None = None   # called with the key of each Worker closed

    def _new(self, tenant, session_id: str, registry) -> Worker:
        """A Worker built from disk (folds its log: run off the loop)."""
        session = self.sessions.get_or_create(tenant.user, tenant.name, session_id)
        return Worker(session, tenant, registry, self.sandbox_factory(),
                      settings=self.settings, admission=self.admission, **self.worker_kwargs)

    def _live(self, key: str) -> Worker | None:
        w = self._workers.get(key)
        return None if w is None or w.closed else w

    def _register(self, key: str, w: Worker) -> Worker:
        self._workers[key] = w
        w.last_active = time.monotonic()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return w
        w.on_loop()
        self._ensure_reaper()
        return w

    def get_or_create(self, tenant, session_id: str, registry) -> Worker:
        """The session's live Worker, looked up by key; only a new one opens
        the session directory and reads the tenant's config (inline: on the
        loop, open() is the non-blocking way). A live Worker keeps its
        config until it is closed or evicted."""
        key = session_key(tenant.user, tenant.name, session_id)
        w = self._live(key)
        if w is None:
            w = self._register(key, self._new(tenant, session_id, registry))
        w.last_active = time.monotonic()
        return w

    async def open(self, tenant, session_id: str, registry) -> Worker:
        """get_or_create for the loop: a new Worker is built (its log folded)
        in a worker thread; concurrent opens of one session share it."""
        key = session_key(tenant.user, tenant.name, session_id)
        w = self._live(key)
        if w is not None:
            w.last_active = time.monotonic()
            return w
        pending = self._opening.get(key)
        if pending is None:
            pending = self._opening[key] = asyncio.ensure_future(
                asyncio.to_thread(self._new, tenant, session_id, registry))
            try:
                built = await asyncio.shield(pending)
            finally:
                self._opening.pop(key, None)
            w = self._live(key) or self._register(key, built)
            if w is not built:
                await built.close()        # someone registered one meanwhile
        else:
            await asyncio.shield(pending)
            w = self._live(key) or self._register(key, pending.result())
        w.last_active = time.monotonic()
        return w

    def get(self, key: str) -> Worker | None:
        return self._workers.get(key)

    def _parked_infos(self, tenant, want: Callable[[dict], bool] | None) -> list[str]:
        """The session ids of `tenant`'s sessions with a vm/parked.json (that
        `want` accepts): only directories that hold one, then their
        session.json identity (DirStore.list_for_tenant). Blocking I/O."""
        out = []
        for meta in self.sessions.list_for_tenant(tenant.user, tenant.name,
                                                  require=PARKED_FILE):
            sid = meta.get("session_id")
            if not sid:
                continue
            if want is not None:
                try:
                    info = json.loads((meta["root"] / PARKED_FILE).read_text())
                    if not want(info):
                        continue
                except (OSError, ValueError):
                    continue
            out.append(sid)
        return out

    async def load_parked(self, tenant, registry,
                          want: Callable[[dict], bool] | None = None) -> list[Worker]:
        """A Worker for each of `tenant`'s parked sessions (vm/parked.json;
        with `want`, those whose parked info it accepts) that has none in
        this process yet, as after an agent-api restart (or an eviction):
        each rebuilds its open device calls from its fold (adopted.py) and
        its question park. The tool-result route calls it for a call_id it
        does not know. Only sessions whose session.json names exactly this
        user and tenant, opened where they are: never a directory of another
        tenant, never a new one. The scan and the builds run off the loop."""
        sids = await asyncio.to_thread(self._parked_infos, tenant, want)
        # concurrently: each opens its own key (open() shares one per key)
        return list(await asyncio.gather(*(
            self.open(tenant, sid, registry) for sid in sids
            if self._live(session_key(tenant.user, tenant.name, sid)) is None)))

    async def load_question_parks(self, tenants, registry) -> list[Worker]:
        """At startup: a Worker for every session parked on a question of
        the direct path, so its answer wait goes on (a restored driver,
        Worker._drive_restored) without waiting for a request to load it."""
        def want(info: dict) -> bool:
            return bool(info.get("question")) and info.get("driver") == DIRECT
        per_tenant = await asyncio.gather(*(self.load_parked(tenant, registry, want=want)
                                            for tenant in tenants))
        return [w for loaded in per_tenant for w in loaded]

    # ------------------------------------------------------------ eviction
    def _ensure_reaper(self) -> None:
        if self.evict_s and self.evict_s > 0 and (self._reaper is None or self._reaper.done()):
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop(),
                                                                  name="workers-reaper")

    async def _reap_loop(self) -> None:
        period = max(0.05, min(30.0, self.evict_s / 4))
        while True:
            await asyncio.sleep(period)
            try:
                await self.evict_idle()
            except Exception:  # noqa: BLE001 - the reaper never dies
                logger.exception("worker eviction failed")

    async def evict_idle(self, grace_s: float | None = None) -> list[str]:
        """Close and forget every Worker evictable (Worker.evictable) and idle
        for `grace_s` (default evict_s). The check and the removal happen
        without yielding, so no request can pick it up in between; a later
        request rebuilds it from disk. The evicted keys."""
        grace = self.evict_s if grace_s is None else grace_s
        now = time.monotonic()
        gone = []
        for key, w in list(self._workers.items()):
            if w.closed:
                self._workers.pop(key, None)
                continue
            if now - w.last_active >= grace and w.evictable():
                self._workers.pop(key, None)
                gone.append((key, w))
        for key, w in gone:
            w.sync.host_event("workflow", {"kind": "evicted", "vm_state": w.vm_state})
            # awaited directly (not gathered): close() marks the Worker closed
            # before its first yield, so no evicted Worker is ever seen open
            await w.close("evict")
            self._closed(key)
        return [k for k, _ in gone]

    async def close(self, key: str) -> None:
        w = self._workers.pop(key, None)
        if w is not None:
            await w.close()
            self._closed(key)

    def _closed(self, key: str) -> None:
        if self.on_closed is not None:
            try:
                self.on_closed(key)
            except Exception:   # the hook never breaks eviction or close
                logger.exception("session %s: on_closed failed", key)

    async def shutdown(self, park_s: float = HARNESS_SHUTDOWN_PARK_S) -> None:
        """Every Worker closed; first (park_s > 0) every running guest is
        parked, concurrently (Worker.shutdown_park), so a restart adopts it."""
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        if park_s > 0:
            workers = [w for w in self._workers.values() if not w.closed]
            await asyncio.gather(*(w.shutdown_park(park_s) for w in workers),
                                 return_exceptions=True)
        # concurrently: each close waits (bounded) for its own log flush and
        # accept thread; one after another, a stuck disk would cost N x that
        await asyncio.gather(*(self.close(key) for key in list(self._workers)),
                             return_exceptions=True)
        # the host-log records still queued (never waited for) reach the disk
        await asyncio.to_thread(durable_writer().flush, CLOSE_FLUSH_TIMEOUT_S)
