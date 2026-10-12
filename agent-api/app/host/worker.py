"""worker — the host half of one session (a direct call, or
Temporal activities).

  Worker(session, tenant, registry, sandbox, sinks=None)
    .act(task_input) -> settle body     task_input = {input_id, text, kind?,
                                        attachments?}
    .attach() -> asyncio.Queue          an SSE sink: every projected SSE string
                                        is put on it; None ends the stream
    .detach(queue)
    .close()                            stop the guest, the proxy, the listeners

act(): build `hello`, start the sandbox, accept :5000, send hello then the
`task` control message, relay every guest event through state_sync (ack after
append) and stream (SSE to the sinks), run host-originated calls through
Sides, heartbeat every HARNESS_HEARTBEAT_S, and return the settle body.
A redelivered input (an input_id already seen) is sent again anyway - the
guest dedupes - and the caller attaches to the running stream; one that
already settled gets its answer replayed from the stored settle, no guest.

One long-lived guest per session.
  vm_state      none -> booting -> running -> parking -> parked -> booting ...
  boot          a fresh session restores the BASE snapshot when the sandbox
                has one (else a rootfs start) and sends hello{resume: false}
  .park(reason) control `park`, wait for the acked `parked`, snapshot to
                {session}/vm/snapshot, vm/parked.json for a restarted worker
  wake          the next act() / ui_response / side_result of a parked
                session restores the session snapshot (no hello; seq, cseq
                continue) and sends the held controls ui_response first;
                fallback base restore + hello{resume: true} + log return
  idle          session_idle_s after a settle: park + snapshot + destroy
  death         link down + sandbox not alive mid-task: base restore +
                hello{resume: true}; the guest pulls the acked log on :5001
                (bulk_server.py), marks open calls op_state{lost}, and the
                worker cancels them (a late side_result is dropped and logged
                input{dedupe: late}); max_resumes per task, then the task fails
  host events   vm{op: boot|restore|snapshot|destroy, source: rootfs|base|
                session, ms}; workflow{parked|resume|idle|heartbeat ...}

Two drivers over one task API. The
Temporal activities (activities.py) are one; act() is the other, in-process
(the direct path). `answer_wait_s` is decided per guest run by the driver of
the task that boots it (answer_wait_for: the tenant's on either path; unset,
0 on the direct path, SessionConfig's 86400 on the workflow path). > 0:
hello limits carry it, the guest parks
itself on a question deadline (parked{reason: question, question_id}), the
worker adopts the park and records `question_park` on the parked state; the
task's wall clock stops while it waits. Both drivers handle that park the
same way: wait up to answer_wait_s for the answer (the workflow's timer;
act()'s _drive), else abort_and_wait(escalated): wake + abort so the guest
settles it, bounded by abort_wait_s.

Across an agent-api restart (a new Worker on the same session directory):
  unsettled     the task's input is acked in the current run, no settle,
                no guest: the task's next start (a run_task retry, a re-sent
                input) resumes that run like a death (ensure_ready) - never
                a new run, never a second turn for a logged llm_response
  parked        vm/parked.json: the parked guest is woken from it; a
                question park comes back as `question_park` with its answer
                deadline, and on the direct path a restored driver
                (_drive_restored) holds the answer wait; device calls the log
                has open are adopted (DeviceCalls keeps each open until its
                result was handed to the guest)
  orphan        a different input on a guest that still holds an unsettled
                task: abort{aborted} settles that one first (_settle_orphan)
  shutdown      Workers.shutdown parks running guests (shutdown_park) so
                the next process can do the above
  .ensure_ready()               boot / wake the guest (prepare_vm)
  .start_task(task_input)       the task of an input, worker-owned (a retry
                                re-attaches to it by input_id); task_input may
                                carry its own wall_clock_s
  .wait_outcome(input_id)       its settle (or the host's failure result), or
                                {outcome: parked, question_id} at a question park
  .on_heartbeat(input_id, beat) beat() on every heartbeat tick of that task
                                (from its start_task, also while queued)
  .drop_queued(input_id, reason) cancel that task while it has not started
                                (waits for its session lock / a slot)
  .answer(question_id, text, input_id)   ui_response to the live/parked guest
  .abort(reason)                abort{reason} to the live/parked guest
  .finish_input(input_id, error, outcome=, question_id=, driver=)
                                the ONLY end of an input: its ticket
                                ({session}/tickets/, write_ticket), then error
                                (if any) + done + end of stream; act() calls it
                                when its task returns, the workflow at deliver
  .replay(input_id, sink=None)  a settled input's answer again, then its end
                                (its settle body; None when not settled)

Both drivers share: the task cap (Workers' Admission, a Slot per Worker:
taken in _act before anything is sent, given back at a question park and
the task's end, taken again by the wake that resumes a parked task), the
wall clock (wall_clock_for: the task input's, else the tenant's
SessionConfig, else wall_clock_s; it goes in the task message, the host
waits it + WALL_CLOCK_GRACE_S counted from when the task took its slot, never
the slot wait), the answer's ui_response input id
(answer_input_id) and the ticket (finish_input -> write_ticket).

Threads touch sockets only (endpoints.py); everything reaches the asyncio
loop through loop.call_soon_threadsafe and one inbound asyncio.Queue that a
pump task drains. The pump is the single writer of state_sync.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from flows.activities import WALL_CLOCK_GRACE_S, SessionConfig
from protocol.allowlist import effective_tools
from protocol.canonical import derive_key_id
from protocol.events import ENUMS
from protocol.messages import Outbox, ProtocolError, ack, control_message

from app.config import (
    HARNESS_ABORT_WAIT_S, HARNESS_ASYNC_TOOLS, HARNESS_CONTEXT_WINDOW_TOKENS, HARNESS_GUEST_ROOT, HARNESS_HEARTBEAT_S,
    HARNESS_HELLO_TIMEOUT_S, HARNESS_HOST_TOOL_TIMEOUT_S, HARNESS_MAX_RESUMES,
    HARNESS_MAX_CONCURRENT_TASKS, HARNESS_MAX_QUEUE_WAIT_S, HARNESS_MAX_TURNS, HARNESS_PARK_GRACE_S, HARNESS_PARK_TIMEOUT_S, HARNESS_PORTS, HARNESS_QUESTION_TIMEOUT_S,
    HARNESS_RESERVE_TOKENS, HARNESS_SANDBOX, HARNESS_SESSION_IDLE_S, HARNESS_SHUTDOWN_PARK_S,
    HARNESS_SPILL_THRESHOLD, HARNESS_STOP_AT_SETTLE,
    HARNESS_TOKEN_BUDGET, HARNESS_WALL_CLOCK_S, LLM_ENABLE_THINKING,
    LLM_MAX_TOKENS, LLM_MODEL, LLM_STREAM, LLM_TEMPERATURE, LLM_URL, MAX_TOOLS,
    MCP_GATEWAY_TOOLS_TIMEOUT_S, MCP_GATEWAY_URL, SYSTEM_PROMPT, TOOL_TIMEOUT,
)
from app.host.admission import Admission
from app.host.bulk_server import BulkServer
from app.gateway.client import GatewayError, fetch_session_tools
from app.host.endpoints import ControlChannel, Endpoints
from app.host.gateway_forwarder import GatewayForwarder, GatewayRelay
from app.host.llm_proxy import LLMProxy
from app.host.sandbox import Sandbox, SandboxError
from app.host.sides import (DEVICE_CALLS, DeviceCalls, Sides, device_content,
                            device_timeout)
from app.host.state_sync import StateSync, StateSyncError
from app.host.stream import DONE, Projector, fatal_items, sse, tokens
from app.session.ids import session_key, session_ref
from app.user.context_builder import ContextBuilder
from app.util.ipc import atomic_write_json

logger = logging.getLogger("host.worker")

HARNESS_VERSION = "0.0.3"
# the guest's settle outcomes (anything else a task returns counts as failed)
SETTLE_OUTCOMES = ENUMS["settle"]["outcome"]


# How a control type is handled outside a plain send (every other type is
# held while parked, the guest wakes for it, and carried over a death resume):
DROPPED_WHILE_PARKED = frozenset({"heartbeat", "park"})
# unacked at a death resume: NOT numbered again after the new hello
NOT_CARRIED = frozenset({"hello", "heartbeat", "park"})
# the only controls that wake a guest parked on a question (send_control)
QUESTION_PARK_WAKERS = ("ui_response", "abort")
# the drivers of a task (task_input["driver"]): act() and the workflow's run_task
DIRECT, WORKFLOW = "direct", "workflow"


def wake_order(item: tuple[str, dict]) -> bool:
    """Sort key of the held controls a wake sends: `ui_response` first,
    the rest in the order they were held."""
    return item[0] != "ui_response"


class GuestError(RuntimeError):
    """The guest died, broke the protocol, or never connected."""


def new_input_id() -> str:
    return "in_" + uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


_TIME_FRAGMENT = re.compile(r"\s*(?:[-*]\s*)?current time:\s*\{current_time\}\.?", re.IGNORECASE)


def strip_current_time(prompt: str) -> str:
    """The system block is fixed per session: remove the {current_time}
    placeholder (and a "Current time:" label with it); a line that held
    nothing else is dropped. The guest puts the time in its per-turn
    preamble."""
    out = []
    for line in prompt.splitlines():
        had = bool(line.strip())
        line = _TIME_FRAGMENT.sub("", line).replace("{current_time}", "").rstrip()
        if had and not line.strip():
            continue
        out.append(line)
    return "\n".join(out)


def session_config(tenant, wall_clock_s: float = HARNESS_WALL_CLOCK_S) -> SessionConfig:
    """The tenant's SessionConfig: the one reader of its SESSION_CONFIG_KEYS
    on both paths (the task signal's config, the workflow's answer timer,
    every task's wall clock). `wall_clock_s` is the wall clock of a tenant
    that sets none (HARNESS_WALL_CLOCK_S); max_queue_wait_s is
    HARNESS_MAX_QUEUE_WAIT_S (run_task's Temporal timeouts only)."""
    entry = tenant.workflow_entry()
    if entry.get("wall_clock_s") is None:
        entry["wall_clock_s"] = int(wall_clock_s)
    cfg = SessionConfig.from_tenant(entry)
    cfg.max_queue_wait_s = max(0, HARNESS_MAX_QUEUE_WAIT_S)
    return cfg


def async_tools_for(tenant) -> bool:
    """hello.config.async_tools: the tenant's `async_tools`, else
    HARNESS_ASYNC_TOOLS."""
    value = getattr(tenant, "async_tools", None)
    return HARNESS_ASYNC_TOOLS if value is None else bool(value)


def answer_input_id(input_id: str, question_id: str) -> str:
    """The ui_response's input_id of an answer to `question_id` asked by
    the task of `input_id`, on either path: stable across retries and
    re-sent answers, so the guest dedupes a second one."""
    return f"{input_id}:answer:{question_id}"


def ticket_id_for(ref, input_id: str) -> str:
    """The ticket of one input: `ticket_` + 24 hex of the canonical hash
    of (user, tenant, session_id, input_id). Not the settle seq: seqs
    restart with every guest run."""
    return derive_key_id("ticket", ref.user, ref.tenant, ref.session_id, input_id)


def make_sandbox() -> Sandbox:
    if HARNESS_SANDBOX == "firecracker":
        from app.host.sandbox.firecracker import FirecrackerSandbox  # type: ignore
        return FirecrackerSandbox()
    from app.host.sandbox.local import LocalSandbox
    return LocalSandbox(HARNESS_GUEST_ROOT)


class Worker:
    def __init__(self, session, tenant, registry, sandbox: Sandbox, sinks=None, *,
                 device_calls: DeviceCalls = DEVICE_CALLS, llm_url: str = LLM_URL,
                 ports: dict = HARNESS_PORTS, hello_timeout_s: float = HARNESS_HELLO_TIMEOUT_S,
                 heartbeat_s: float = HARNESS_HEARTBEAT_S,
                 wall_clock_s: float = HARNESS_WALL_CLOCK_S,
                 stop_at_settle: bool = HARNESS_STOP_AT_SETTLE,
                 session_idle_s: float | None = HARNESS_SESSION_IDLE_S,
                 park_timeout_s: float = HARNESS_PARK_TIMEOUT_S,
                 max_resumes: int = HARNESS_MAX_RESUMES,
                 park_grace_s: float = HARNESS_PARK_GRACE_S,
                 gateway_url: str = MCP_GATEWAY_URL,
                 gateway_tools_timeout_s: float = MCP_GATEWAY_TOOLS_TIMEOUT_S,
                 abort_wait_s: float = HARNESS_ABORT_WAIT_S,
                 admission: Admission | None = None) -> None:
        self.session = session
        self.tenant = tenant
        self.registry = registry
        self.sandbox = sandbox
        self.sinks: list[asyncio.Queue] = list(sinks or [])
        self._sink_input: dict[int, str] = {}     # id(queue) -> the input it streams
        self.device_calls = device_calls
        self.llm_url = llm_url
        self.ports = dict(ports)
        self.hello_timeout_s = hello_timeout_s
        self.heartbeat_s = heartbeat_s
        self.wall_clock_s = wall_clock_s
        self.stop_at_settle = stop_at_settle
        self.session_idle_s = session_idle_s
        self.park_timeout_s = park_timeout_s
        self.max_resumes = max_resumes
        self.park_grace_s = park_grace_s
        self.gateway_url = gateway_url
        self.gateway_tools_timeout_s = gateway_tools_timeout_s
        self.abort_wait_s = abort_wait_s

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
        self._channel: ControlChannel | None = None
        self._link_up = asyncio.Event()
        self._outbox: Outbox | None = None
        self._guest_run = 0          # increments on every sandbox start
        self._stop_accept = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._task_lock = asyncio.Lock()
        # the running task: input_id, its settle, its wall clock (hello limits)
        self._current: tuple[str, asyncio.Future, float] | None = None
        self.last_hello: dict | None = None
        self.closed = False
        # park, resume, idle, death
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
        # (0 = the guest denies). Decided per task run by the driver that
        # boots the guest (answer_wait_for, _boot)
        self.answer_wait_s = self.answer_wait_for(DIRECT)
        self._changed = asyncio.Event()             # replaced (and set) on every change
        # none | booting | running | parking | parked; the session snapshot
        # itself is the sandbox's business (has_snapshot / restore)
        self._vm_state = "none"
        # {input_id, question_id}: a field of the parked state (vm_state clears it)
        self._question_park: dict | None = None
        self.snapshot_dir = session.root / "vm" / "snapshot"
        self.parked_file = session.root / "vm" / "parked.json"
        self._parked_evt = asyncio.Event()
        self._vm_lock = asyncio.Lock()
        self._deferred: list[tuple[str, dict]] = []  # controls held while parked
        self._wake_task: asyncio.Task | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._resumes = 0                           # death resumes of the running task
        self._lost_calls: set[str] = set()
        self._side_tasks: dict[str, asyncio.Task] = {}
        self._bg: set[asyncio.Task] = set()         # fire-and-forget tasks (strong refs)
        self._acts: dict[str, asyncio.Task] = {}    # input_id -> its task (start_task)
        self._beats: dict[str, set[Callable[[], None]]] = {}   # input_id -> on_heartbeat
        # call_id -> its deliver, for the device calls this Worker adopted
        self._adopted: dict[str, Callable[[str], None]] = {}
        self._delivered: set[str] = set()           # adopted calls whose result went out
        # the driver of the running task (direct | workflow; None: not given)
        self._task_driver: str | None = None
        # a question park's answer deadline (epoch ms; the direct path's
        # answer wait, kept in vm/parked.json across a restart)
        self._answer_deadline_ms: int | None = None
        self._park_driver: str | None = None        # the driver of a restored question park
        # (input_id, task): the direct path's driver of a question park this
        # process restored from vm/parked.json (_drive_restored)
        self._restored: tuple[str, asyncio.Task] | None = None
        self._load_parked()
        self._adopt_open_device_calls()
        self._resume_restored_park()

    def _spawn(self, coro) -> asyncio.Task:
        """A background task the loop would otherwise only hold weakly."""
        task = self._loop.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    # ------------------------------------------------------------ sinks
    def attach(self, input_id: str | None = None) -> asyncio.Queue:
        """A new sink. With `input_id` it only receives that input's stream
        (tokens, error, done, end-of-stream): two requests can be queued on
        one worker, and the earlier task's end must not close the later
        request's stream (chat.py: a task outlives a client disconnect)."""
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

    # ------------------------------------------------------------ the task API
    @property
    def vm_state(self) -> str:
        return self._vm_state

    @vm_state.setter
    def vm_state(self, value: str) -> None:
        """none | booting | running | parking | parked. `question_park` is a
        field of the parked state: any other state clears it. Every change
        wakes the waiters (_wait)."""
        if value == self._vm_state:
            return
        self._vm_state = value
        if value != "parked":
            self._question_park = None
        self._pulse()

    @property
    def question_park(self) -> dict | None:
        return self._question_park

    @question_park.setter
    def question_park(self, value: dict | None) -> None:
        """{input_id, question_id} while the guest is parked on that
        question; a change wakes the waiters (_wait)."""
        if value == self._question_park:
            return
        self._question_park = value
        self._pulse()

    def _pulse(self) -> None:
        evt, self._changed = self._changed, asyncio.Event()
        evt.set()

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
        `wall_clock_s` (SessionConfig), else this Worker's `wall_clock_s`
        (HARNESS_WALL_CLOCK_S)."""
        return session_config(self.tenant, self.wall_clock_s).wall_clock_s

    def wall_clock_for(self, task_input: dict) -> int:
        """The wall clock of `task_input`'s task: its own `wall_clock_s`
        (the workflow's RunTask), else default_wall_clock_s(). It goes to
        the guest in the `task` message (the guest settles `timeout` at
        it); the host waits it + WALL_CLOCK_GRACE_S (_wait) from when the
        task started (its slot taken), on both paths."""
        return int(task_input.get("wall_clock_s") or self.default_wall_clock_s())

    def open_questions(self) -> list[dict]:
        """Unanswered questions of the current run whose call has no result
        yet, as the SSE `question` payload."""
        out = []
        state = self.sync.state
        for qid, q in state.open_questions.items():
            call = state.open_calls.get(q["call_id"])
            body = self.sync.question_bodies.get(qid)
            if q.get("answered") or call is None or "result" in call or body is None:
                continue
            out.append({k: body.get(k) for k in ("question_id", "call_id", "text",
                                                 "options", "deadline")})
        return out

    async def ensure_ready(self, input_id: str | None = None, driver: str | None = None) -> None:
        """A guest that can take a control: wake a parked one, boot one
        that is not running (prepare_vm; also the start of every task).
        `driver` (DIRECT | WORKFLOW) decides a booted guest's answer_wait_s.

        `input_id` is the task about to run: when the current run holds it
        unsettled (its input was acked, no settle: the process that drove it
        died, or its guest went away), the boot resumes that run instead of
        opening a new one - hello{resume: true} and the log return, as after
        a guest death: open calls end op_state{lost}, a turn whose
        llm_response is logged is never asked again."""
        self._ensure_plumbing()
        await self._slot_for_wake()
        async with self._vm_lock:        # waits out a park in progress
            if self.vm_state == "parked":
                await self._wake_locked()
            elif not self._guest_ready():
                resume = input_id is not None and self.sync.open_input == input_id
                if resume:
                    self.sync.host_event("workflow", {"kind": "resume", "reason": "unsettled_input",
                                                      "input_id": input_id,
                                                      "acked_seq": self.sync.last_seq})
                await self._boot(resume=resume, driver=driver)

    def start_task(self, task_input: dict) -> asyncio.Task:
        """The task of `task_input` (queued behind the running one), owned by
        the worker: the driver that started it may be cancelled or fail, the
        task runs on, and a retry (same input_id) gets the same task back."""
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
            if input_id in self.sync.settled:
                return self.sync.settled[input_id]
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

    async def _until(self, done: Callable[[], bool], aw: asyncio.Future,
                     timeout: float | None = None) -> float:
        """Wait until `done()` holds (checked on every change, _pulse), `aw`
        is done or `timeout` (None: no limit) ran out; the time left."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not done() and not aw.done():
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return 0.0
            await _first(aw, self._changed, timeout=left)
        return float("inf") if deadline is None else max(0.0, deadline - time.monotonic())

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

    async def ensure_parked(self, reason: str) -> bool:
        """park_vm: parked and snapshotted. An adopted park in progress
        finishes first; a guest still running is asked to park."""
        self._ensure_plumbing()
        async with self._vm_lock:
            pass
        if self.vm_state == "parked":
            return True
        return await self.park(reason)

    async def settle_park(self) -> None:
        """Wait out a park in progress (vm_state parking, bounded by
        park_timeout_s): an answer then sees the question park it ends,
        instead of going to a guest that is closing its link."""
        while True:
            evt = self._changed
            if self.vm_state != "parking":
                return
            await evt.wait()

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
            await asyncio.wait_for(asyncio.shield(task), timeout=self.abort_wait_s)
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
        atomic_write_json(path, {
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

    # ------------------------------------------------------------ hello
    def tool_entries(self) -> list[dict]:
        """hello.config.tools: host/device tools, then the gateway's mcp
        entries verbatim (fetched by load_gateway_entries)
        except one named like a host tool. Guest built-ins are implicit."""
        host = self.host_tool_entries()
        names = {e["name"] for e in host}
        gateway = [e for e in self.gateway_entries or [] if e.get("name") not in names]
        if len(gateway) != len(self.gateway_entries or []):
            logger.warning("session %s: gateway tools named like host tools dropped",
                           self.session_key)
        return host + gateway

    async def load_gateway_entries(self) -> list[dict]:
        """GET /internal/tools for this session (async httpx, short timeout).
        The first successful answer is kept for the session: the gateway
        pins it, and a resume hello reuses it even when the gateway is down
        then. A failure is logged and the session starts without MCP tools
        (the next boot tries again)."""
        if self.gateway_entries is not None or not self.gateway_url:
            return self.gateway_entries or []
        try:
            entries = await fetch_session_tools(self.user, self.tenant.name, self.session_id,
                                                base_url=self.gateway_url,
                                                timeout_s=self.gateway_tools_timeout_s)
        except GatewayError as e:
            logger.warning("session %s: starting without MCP tools (%s)", self.session_key, e)
            return []
        self.gateway_entries = entries
        return entries

    def host_tool_entries(self) -> list[dict]:
        """Host/device tools: the allowlist applied to the registry's
        skills, each with its OpenAI schema (keyword prefilter retired)."""
        allowed = self.tenant.allowed_skill_names()
        server = self.registry.get_server_tool_names(allowed)
        device = self.registry.get_device_tool_names(allowed)
        entries: list[dict] = []
        seen: set[str] = set()
        for d in self.registry.get_active_tool_definitions(allowed):
            name = d["function"]["name"]
            if name in seen:
                continue
            side = "host" if name in server else "device" if name in device else None
            if side is None:
                continue
            seen.add(name)
            entries.append({"name": name, "side": side, "pin": None,
                            "schema": d, "description": d["function"].get("description", "")})
        return entries

    def system_block(self) -> dict:
        cb = ContextBuilder(user_id=self.user, session=self.session,
                            base_prompt=self.tenant.system_prompt)
        user_idx = cb._read_if_nonempty(cb.user_memory.index)
        session_idx = cb._read_if_nonempty(cb.session_memory.index)
        index_parts = []
        if user_idx:
            index_parts.append("USER MEMORY INDEX:\n" + user_idx)
        if session_idx:
            index_parts.append("SESSION MEMORY INDEX:\n" + session_idx)
        return {
            "base_prompt": strip_current_time(self.tenant.system_prompt or SYSTEM_PROMPT),
            "profile": cb.profile.to_prompt_block(),
            "memory_index": "\n\n".join(index_parts),
        }

    def attachments(self, text: str) -> list[dict]:
        """Recalled memory bodies for the input's preamble."""
        cb = ContextBuilder(user_id=self.user, session=self.session,
                            base_prompt=self.tenant.system_prompt)
        recalled = cb.user_memory.recall(text) + cb.session_memory.recall(text)
        return [{"kind": "memory", "type": r.type, "slug": r.slug, "name": r.name,
                 "body": r.body.strip()} for r in recalled[:3]]

    def limits(self) -> dict:
        return {
            # the default only: every task message carries its own
            "wall_clock_s": int(self.default_wall_clock_s()),
            "max_tools": self.tenant.max_tools or MAX_TOOLS,
            "max_turns": HARNESS_MAX_TURNS,
            "max_tokens": self.tenant.max_tokens or LLM_MAX_TOKENS,
            "op_timeout_s": {"guest": 120, "host": HARNESS_HOST_TOOL_TIMEOUT_S,
                             "device": TOOL_TIMEOUT, "mcp": 300},
            "spill_threshold": HARNESS_SPILL_THRESHOLD,
            # the guest compacts when its estimate exceeds window - reserve
            # (the token budget is a proxy knob, not a guest limit)
            "context_window_tokens": HARNESS_CONTEXT_WINDOW_TOKENS,
            "reserve_tokens": HARNESS_RESERVE_TOKENS,
            # running guest ops get this long after `park`, then killed
            "park_grace_s": self.park_grace_s,
            # > 0 = a question deadline parks the task (either driver)
            "answer_wait_s": int(self.answer_wait_s),
        }

    def answer_wait_for(self, driver: str | None) -> int:
        """limits.answer_wait_s of a guest booted for a task of `driver`
        (DIRECT | WORKFLOW). The tenant's answer_wait_s when it sets one, on
        either path. Unset: the workflow path takes SessionConfig's default
        (86400; the workflow's durable timer waits for the answer), the
        direct path 0, the guest denies an unanswered question (as before
        question parks: no durable timer holds a direct park, and a 24 h
        park would hold the client's request). Applied where the hello is
        built (_boot), so per guest run: a guest booted by one driver keeps
        its value for later tasks (a wake keeps its hello)."""
        explicit = self.tenant.workflow_entry().get("answer_wait_s")
        if explicit is not None:
            return int(explicit)
        return SessionConfig().answer_wait_s if driver == WORKFLOW else 0

    def build_hello(self, resume: bool = False) -> dict:
        params: dict = {"max_tokens": self.tenant.max_tokens or LLM_MAX_TOKENS,
                        "temperature": LLM_TEMPERATURE,
                        "stream": LLM_STREAM}      # false = one call per turn
        if not LLM_ENABLE_THINKING:
            params["chat_template_kwargs"] = {"enable_thinking": False}
        config = {
            "model": LLM_MODEL,
            "params": params,
            "tools": self.tool_entries(),
            "limits": self.limits(),
            "hooks": {},
            "async_tools": async_tools_for(self.tenant),
            "exclude_builtins": list(self.tenant.exclude_builtins),
            "question_timeout_s": HARNESS_QUESTION_TIMEOUT_S,
            "ui_attached": True,
            "ports": dict(self.ports),
            "harness_version": HARNESS_VERSION,
        }
        effective_tools(config)  # raises ValueError on a bad tool list, before anything runs
        return {
            "config": config,
            "session_id": self.session_id,
            "principal": {"user": self.tenant.user, "tenant": self.tenant.name},
            "system_block": self.system_block(),
            "resume": resume,
            "at": now_ms(),
        }

    # ------------------------------------------------------------ control
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
                      declared lost (logged `input{dedupe: late}`),
                      or a heartbeat / park to a parked guest
          "no_guest"  no guest run can take it (never booted, or gone)"""
        if type == "side_result" and body.get("call_id") in self._lost_calls:
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
    def _ensure_plumbing(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._inbound = asyncio.Queue()
        if self.endpoints is None:
            if self.gateway_url and "gateway" in self.ports:
                self.gateway_relay = GatewayRelay(self.gateway_url, user=self.user,
                                                  tenant=self.tenant.name,
                                                  session=self.session_id,
                                                  host_event=self.sync.host_event)
            # no gateway: no :5011 listener at all (the guest has no mcp tools)
            listen = (self.ports if self.gateway_relay is not None
                      else {k: v for k, v in self.ports.items() if k != "gateway"})
            self.endpoints = Endpoints(self.session.root, listen)
            self.proxy = LLMProxy(self.endpoints.listener(self.ports["llm"]), self.llm_url,
                                  self.sync.host_event, token_budget=HARNESS_TOKEN_BUDGET)
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
                               session_ref=self.ref)
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
            conn = endpoints.accept_control(timeout=None)
            if conn is None:
                continue                # closed: the loop condition ends it
            n += 1
            ch = ControlChannel(conn, self._loop, self._inbound, name=f":{n}")
            self._loop.call_soon_threadsafe(self._inbound.put_nowait, ("link", "up", ch))

    # ------------------------------------------------------------ vm lifecycle
    def _vm_event(self, op: str, source: str | None, t0: float, **extra) -> None:
        """vm{op, source, ms}: ms is the sandbox's own figure for the op when
        it reports one (`last_vm_event`), else the host's wall time; ready_ms
        is the host's (restore/boot: until the guest's link is up)."""
        ready_ms = int((time.monotonic() - t0) * 1000)
        body = {"op": op, "ms": ready_ms, "ready_ms": ready_ms, "run": self._guest_run,
                "sandbox": type(self.sandbox).__name__, **extra}
        if source is not None:
            body["source"] = source
        sb = self.sandbox.last_vm_event
        if isinstance(sb, dict) and sb.get("op") == op and isinstance(sb.get("ms"), (int, float)):
            body["ms"] = int(sb["ms"])
            if sb.get("source") and source is None:
                body["source"] = sb["source"]
        self.sync.host_event("vm", body)

    def _close_guest_ports(self) -> None:
        """No :5000/:5001/:5011 listener while the guest is parked: its
        idle-wait loop must not reconnect before the snapshot (sandbox
        finding). Held gateway calls keep running in the relay."""
        if self.endpoints is None:
            return
        self.endpoints.close_port(self.ports["control"])
        if self.gateway_relay is not None:
            if self.gateway is not None:
                self.gateway.stop(join=False)
                self.gateway = None
            self.endpoints.close_port(self.ports["gateway"])
        if "bulk" in self.ports:
            if self.bulk is not None:
                self.bulk.stop(join=False)
                self.bulk = None
            self.endpoints.close_port(self.ports["bulk"])

    def _open_guest_ports(self) -> None:
        """Listen again before a restore or boot."""
        if self.endpoints is None:
            return
        self.endpoints.reopen(self.ports["control"])
        if "bulk" in self.ports and self.bulk is None:
            self.bulk = BulkServer(self.endpoints.reopen(self.ports["bulk"]), self.session.root,
                                   self.session_id, self.sync.read,
                                   on_put=self._on_bulk_put).start()
        if self.gateway_relay is not None and self.gateway is None:
            self.gateway = GatewayForwarder(self.endpoints.reopen(self.ports["gateway"]),
                                            self.gateway_relay).start()

    def _drop_link(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._link_up.clear()

    async def _wait_link(self) -> bool:
        try:
            await asyncio.wait_for(self._link_up.wait(), timeout=self.hello_timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def _start_guest(self, snapshot_dir: Path | None) -> tuple[str, str, float] | None:
        """Start a guest run: restore `snapshot_dir` (None: the BASE snapshot)
        and wait for its link. A base that cannot be restored falls back to a
        rootfs start; a session snapshot that cannot be restored, or a guest
        that never connects, gives None (vm{ok: false} logged, sandbox
        stopped). Returns (op, source, t0) for _mark_running."""
        t0 = time.monotonic()
        self._drop_link()
        if self.sandbox.is_alive():
            await asyncio.to_thread(self.sandbox.stop)
        self._guest_run += 1
        self.vm_state = "booting"
        self._open_guest_ports()
        op, source = "restore", "base" if snapshot_dir is None else "session"
        try:
            # restore validates before launching: no has_snapshot() pre-check
            await asyncio.to_thread(self.sandbox.restore, self.session.root, snapshot_dir)
        except SandboxError as e:
            logger.warning("session %s: %s restore failed (%s)", self.session_key, source, e)
            if snapshot_dir is not None:
                self._vm_event(op, source, t0, ok=False, error=str(e)[:200])
                return None
            op, source = "boot", "rootfs"
            await asyncio.to_thread(self.sandbox.start, self.session.root)
        if not await self._wait_link():
            self._vm_event(op, source, t0, ok=False, error="no connection")
            await asyncio.to_thread(self.sandbox.stop)
            return None
        return op, source, t0

    def _mark_running(self, op: str, source: str, t0: float,
                      held: list[tuple[str, dict]], **extra) -> None:
        """The shared tail of a boot and a wake: running, no parked file, the
        vm event, then the held controls `ui_response` first."""
        self.vm_state = "running"
        self._clear_parked_file()
        self._vm_event(op, source, t0, **extra)
        self._send_held(held)

    async def _boot(self, *, resume: bool, driver: str | None = None) -> None:
        """Start a guest run and send hello. Fresh (resume=False): a new log
        run, cseq from 1. Resume (death, or a park whose snapshot is gone):
        cseq continues, `hello{resume: true}`, the guest takes the log return
        on :5001 and continues the run's seq; the unacked controls of the
        previous guest are numbered again after the hello. Base snapshot
        restore when the sandbox has one, else a rootfs boot (start).
        A fresh boot whose current run is a migrated legacy history no
        guest continued yet (StateSync.migrated_run) is a resume: the guest
        folds the migrated run, so the prior conversation is in context.
        `driver`: the task's (answer_wait_for); None keeps the previous
        guest's answer_wait_s (a death resume, a wake's fallback)."""
        if driver is not None:
            self.answer_wait_s = self.answer_wait_for(driver)
        carried: list[tuple[str, dict]] = []
        migrated = not resume and self.sync.migrated_run
        resume = resume or migrated
        if resume:
            highest = 0
            if self._outbox is not None:
                carried = [(m["type"], m["body"]) for m in self._outbox.pending
                           if m["type"] not in NOT_CARRIED]
                highest = self._outbox.highest_sent
            self._outbox = Outbox(self.session_id, "cseq")
            self._outbox.resume_from(highest)
        else:
            self.sync.begin_run()
            self._outbox = Outbox(self.session_id, "cseq")   # a fresh guest's inbox starts at 0
        held, self._deferred = self._deferred, []
        try:
            started = await self._start_guest(None)
        except BaseException:
            self.vm_state = "none"
            self._deferred = held + self._deferred
            raise
        if started is None:
            self.vm_state = "none"
            self._deferred = held + self._deferred
            raise GuestError(f"guest did not connect within {self.hello_timeout_s}s")
        await self.load_gateway_entries()
        hello = self.build_hello(resume=resume)
        if migrated:
            # the migrated run's allowlist holds only its legacy tools:
            # re-pin it to this hello's before the guest takes the log return
            self.sync.retool_migrated(effective_tools(hello["config"]))
        self.last_hello = hello
        self.send_control("hello", hello)
        self._mark_running(*started, carried + held, resume=resume)

    async def _destroy(self, reason: str) -> None:
        """Stop the VM. A parked session stays parked (its snapshot is on disk)."""
        t0 = time.monotonic()
        self._drop_link()
        await asyncio.to_thread(self.sandbox.stop)
        if self.vm_state != "parked":
            self.vm_state = "none"
        self._vm_event("destroy", None, t0, reason=reason)

    def _guest_ready(self) -> bool:
        # "running" is set only once hello went to this run's guest (or a
        # restored guest that keeps its hello reconnected): see _mark_running
        return (self.vm_state == "running" and self.sandbox.is_alive()
                and self._channel is not None and not self._channel.closed)

    # ------------------------------------------------------------ park / wake
    async def park(self, reason: str = "requested", *, idle: bool = False) -> bool:
        """Park the guest: send `park`, wait for the acked
        `parked`, snapshot the VM to {session}/vm/snapshot. True when parked.
        The next act()/ui_response/side_result restores it.

        idle=True (the idle timer): only while no task runs, and the VM
        is destroyed afterwards (also a guest that could not park)."""
        self._ensure_plumbing()
        async with self._vm_lock:
            if idle and (self._task_lock.locked() or self._current_future() is not None):
                return False            # a task arrived: not idle any more
            parked = False
            if self._guest_ready():
                self.vm_state = "parking"
                self._parked_evt.clear()
                self.send_control("park", {"reason": reason})
                parked = await self._park_locked(reason)
            if idle and not self._task_lock.locked() and (parked or self.sandbox.is_alive()):
                await self._destroy("idle")
            return parked

    async def _park_locked(self, reason: str, question: dict | None = None) -> bool:
        """Wait for the acked `parked`, snapshot, parked. `question`
        ({input_id, question_id}): the guest parked on that question."""
        try:
            await asyncio.wait_for(self._parked_evt.wait(), timeout=self.park_timeout_s)
        except asyncio.TimeoutError:
            pass
        if self.vm_state != "parking":
            return False                # the guest died meanwhile: the death path owns it
        if not self._parked_evt.is_set():
            logger.warning("session %s: no parked within %ss", self.session_key,
                           self.park_timeout_s)
            self.sync.host_event("workflow", {"kind": "park_timeout", "reason": reason})
            self.vm_state = "running"
            return False
        t0 = time.monotonic()
        ok, error = True, None
        try:
            await asyncio.to_thread(self.sandbox.snapshot, self.snapshot_dir)
        except Exception as e:
            ok, error = False, str(e) or type(e).__name__
            logger.warning("session %s: snapshot failed (%s); a resume will replay the log",
                           self.session_key, error)
        extra = {"ok": ok} if ok else {"ok": ok, "error": error[:200]}
        self._vm_event("snapshot", "session", t0, **extra)
        if not ok:
            await asyncio.to_thread(self.sandbox.stop)   # its memory is lost: resume from the log
        self._drop_link()
        self.vm_state = "parked"
        # the answer wait runs from the park: a restarted process resumes
        # the direct path's wait with what is left (vm/parked.json)
        self._answer_deadline_ms = (now_ms() + int(self.answer_wait_s * 1000)
                                    if question else None)
        self._park_driver = self._task_driver if question else None
        if question:
            self._slot.release()        # waiting for an answer is not work
        self.question_park = question
        cp = self.sync.state.parked_at
        self._write_parked_file(reason, cp)
        self.sync.host_event("workflow", {"kind": "parked", "reason": reason, "checkpoint_seq": cp,
                                          "snapshot": ok,
                                          **({"question_id": question["question_id"]}
                                             if question else {})})
        return True

    async def _adopt_park(self, reason: str, question_id: str | None = None,
                          input_id: str | None = None) -> None:
        """The guest parked on its own (a question deadline with
        answer_wait_s > 0): snapshot it; a question park is what
        wait_outcome() reports for the input."""
        question = ({"input_id": input_id, "question_id": question_id}
                    if reason == "question" and question_id and input_id else None)
        async with self._vm_lock:
            if self.vm_state == "parking":
                await self._park_locked(reason, question)

    def _request_wake(self) -> None:
        if self._loop is None or self.closed:
            return
        if self._wake_task is None or self._wake_task.done():
            self._wake_task = self._spawn(self._wake_guarded())

    async def _wake_guarded(self) -> None:
        try:
            await self.wake()
        except Exception as e:
            logger.exception("session %s: wake failed", self.session_key)
            fut = self._current_future()
            if fut is not None:
                fut.set_exception(GuestError(f"resume after park failed: {e}"))

    async def wake(self) -> None:
        await self._slot_for_wake()
        async with self._vm_lock:
            await self._wake_locked()

    async def _slot_for_wake(self) -> None:
        """A parked guest woken for the task this process holds (a
        question park's answer or abort, prepare_vm of the resume) takes
        the session's slot first; the task's end gives it back. An abort
        never waits for a slot (it only makes the guest settle). A slot taken
        after the task ended (or the worker closed) meanwhile goes back."""
        if self.vm_state != "parked" or self._current_future() is None:
            return
        if any(t == "abort" for t, _ in self._deferred):
            return
        await self._slot.acquire()
        if self.closed or self._current_future() is None:
            self._slot.release()

    async def _wake_locked(self) -> None:
        """Resume a parked guest: restore its session snapshot (no hello, seq
        and cseq continue; the link handshake resends what it has not acked),
        then send the held controls `ui_response` first. Fallback (no usable
        session snapshot, or no link): base restore (or rootfs boot) +
        hello{resume: true} + the log return."""
        if self.vm_state != "parked":
            return
        self._cancel_idle()
        started = await self._start_guest(self.snapshot_dir)
        if started is None:
            await self._boot(resume=True)
            return
        held, self._deferred = self._deferred, []
        self._mark_running(*started, held)

    # ------------------------------------------------------------ idle
    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self.session_idle_s is None or self.session_idle_s <= 0 or self._loop is None:
            return
        self._idle_handle = self._loop.call_later(
            self.session_idle_s, lambda: self._spawn(self._idle_park()))

    async def _idle_park(self) -> None:
        """session_idle_s after a settle: park + snapshot + destroy."""
        self._idle_handle = None
        if self.closed:
            return
        self.sync.host_event("workflow", {"kind": "idle", "idle_s": self.session_idle_s})
        try:
            await self.park("idle", idle=True)
        except Exception:
            logger.exception("session %s: idle park failed", self.session_key)

    # ------------------------------------------------------------ parked file
    def _write_parked_file(self, reason: str, checkpoint_seq) -> None:
        """What a new Worker (agent-api restart) needs to resume the parked
        guest: cseq continues, the guest's answer_wait_s; for a question
        park its {input_id, question_id}, the answer deadline (epoch ms) and
        the driver of the task (direct: the new process drives the answer
        wait itself, _drive_restored). Whether the session snapshot is still
        usable is the sandbox's to say (restore() refuses one that is not)."""
        info = {"cseq": self._outbox.highest_sent if self._outbox is not None else 0,
                "reason": reason, "checkpoint_seq": checkpoint_seq,
                "last_seq": self.sync.last_seq, "at": now_ms(),
                "answer_wait_s": self.answer_wait_s}
        if self.question_park is not None:
            info.update(question=dict(self.question_park), driver=self._park_driver,
                        answer_deadline_ms=self._answer_deadline_ms)
        try:
            atomic_write_json(self.parked_file, info)
        except OSError as e:
            logger.warning("could not write %s: %s", self.parked_file, e)

    def _clear_parked_file(self) -> None:
        try:
            self.parked_file.unlink()
        except FileNotFoundError:
            pass

    def _load_parked(self) -> None:
        try:
            info = json.loads(self.parked_file.read_text())
        except (OSError, ValueError):
            return
        self.vm_state = "parked"
        self._outbox = Outbox(self.session_id, "cseq")
        self._outbox.resume_from(int(info.get("cseq", 0)))
        if info.get("answer_wait_s") is not None:
            self.answer_wait_s = info["answer_wait_s"]   # the parked guest's hello
        q = info.get("question")
        if isinstance(q, dict) and q.get("input_id") and q.get("question_id") \
                and q["input_id"] not in self.sync.settled:
            self._question_park = {"input_id": q["input_id"], "question_id": q["question_id"]}
            self._answer_deadline_ms = info.get("answer_deadline_ms")
            self._park_driver = info.get("driver")

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
        never = asyncio.get_running_loop().create_future()
        try:
            await self._until(lambda: self.question_park != park, never, self.answer_left_s())
        finally:
            never.cancel()
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

        def _finish(t: asyncio.Task) -> None:
            outcome = task_outcome(t)
            self.finish_input(input_id, end_error(t, outcome), outcome=outcome, driver=DIRECT)
        task.add_done_callback(_finish)
        return await self._drive(input_id, task)

    # ------------------------------------------------------------ restart
    def _adopt_open_device_calls(self) -> None:
        """A new Worker on a parked session (an agent-api restart): the
        device calls the log still has open (tool_call side device, no
        result) are rebuilt from the fold into device_calls, so a POST
        /tool-result after the restart is accepted (_deliver_adopted) and a
        re-attaching stream re-emits them. Only for a parked run: the guest
        then resumes from that park (session snapshot, or base restore + a
        log return ending with `parked`) and its side calls wait again; a
        guest that died mid-run marks them lost on its resume (409)."""
        state = self.sync.state
        if self.vm_state != "parked" or not state.parked:
            return
        open_calls = {cid: c for cid, c in state.open_calls.items()
                      if c["side"] == "device" and "result" not in c}
        if not open_calls:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        first = min(c["seq"] for c in open_calls.values())
        for ev in self.sync.read(first - 1):
            body = ev["body"]
            if ev["type"] == "tool_call" and body["call_id"] in open_calls:
                cid, name = body["call_id"], body["name"]
                deliver = functools.partial(self._deliver_adopted, cid, name)
                self._adopted[cid] = deliver
                self.device_calls.adopt(cid, name, body.get("args", {}), self.ref,
                                        loop=loop, deliver=deliver)
                if loop is not None:
                    # no Sides timer waits on it: the same limit as a live
                    # call, from the adoption (the parked guest's timers do
                    # not run until something wakes it)
                    loop.call_later(TOOL_TIMEOUT, self._adopted_timeout, cid, name)
                self.sync.host_event("side_call", {"side": "device", "call_id": cid,
                                                   "name": name, "state": "adopted"})

    def _adopted_timeout(self, call_id: str, name: str) -> None:
        if call_id in self._adopted and not self.closed:
            self.device_calls.resolve(call_id, device_timeout(name, TOOL_TIMEOUT))

    def _deliver_adopted(self, call_id: str, name: str, result: str) -> None:
        """The result of an adopted device call: side_result to the guest
        (held while parked: the guest wakes for it, as for any control).
        Raises when no guest run can take it (a closed Worker, no guest):
        DeviceCalls keeps the call open and the device may POST again. Once
        handed over, the call_id is remembered: a second delivery is not
        sent again (the guest would ignore it too: a side_result for a
        terminal call)."""
        if call_id in self._delivered:
            return
        if self.closed:
            raise GuestError("worker closed")
        self._ensure_plumbing()
        content, ok = device_content(result)
        status = self.send_control("side_result", {"call_id": call_id, "side": "device",
                                                   "content": content, "ok": ok})
        if status == "no_guest":
            raise GuestError("no guest run takes the result")
        self._delivered.add(call_id)
        self._adopted.pop(call_id, None)
        try:
            self.sync.host_event("side_call", {"side": "device", "call_id": call_id,
                                               "name": name, "ok": ok, "adopted": True,
                                               "sent": status})
        except Exception:       # delivered: the trace never makes it a failure
            logger.exception("session %s: side_call host event", self.session_key)

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

        def _finish(t: asyncio.Task) -> None:
            outcome = task_outcome(t)
            self.finish_input(input_id, end_error(t, outcome), outcome=outcome, driver=DIRECT)
        # before the waiters' callbacks: the streams end before act() returns
        task.add_done_callback(_finish)
        return await self._drive(input_id, task)

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
                                  f"No settle within {self.abort_wait_s:g}s of abort{{escalated}}.")

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
        try:
            self._ensure_plumbing()
        except Exception as e:      # e.g. the unix socket path is too long
            logger.exception("session %s: host plumbing failed", self.session_key)
            message = f"The agent sandbox failed: {e}"
            self._fail(input_id, "failed", message)
            raise GuestError(message) from e
        async with self._task_lock:     # first: queued behind its session, no slot held
            body = self.sync.settled.get(input_id)
            if body is not None:
                return self._settled_again(input_id, body)
            try:
                # the global cap (Workers' Admission), before anything is
                # sent; given back at a question park (_park_locked), taken
                # again by the wake that resumes it (_slot_for_wake)
                await self._slot.acquire()
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

    def _task_message(self, task_input: dict) -> tuple[str, dict]:
        kind = task_input.get("kind", "task")
        if kind not in ("task", "follow_up", "steer"):
            kind = "task"
        attachments = task_input.get("attachments")
        if attachments is None:
            attachments = self.attachments(task_input.get("text", ""))
        return kind, {"input_id": task_input["input_id"], "text": task_input.get("text", ""),
                      "attachments": attachments,
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
            if self.vm_state != "parked" and (self.stop_at_settle or not guest_settled
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
        status = self.send_control("abort", {"reason": "aborted"})
        self.sync.host_event("workflow", {"kind": "orphan_abort", "input_id": orphan,
                                          "before": input_id, "sent": status})
        if status == "no_guest":
            return
        never = self._loop.create_future()
        try:
            await self._until(lambda: orphan in self.sync.settled, never, self.abort_wait_s)
        finally:
            never.cancel()
        async with self._vm_lock:
            await self._destroy("orphan")
            self._clear_parked_file()
            self.vm_state = "none"          # not the parked guest that holds it again
            if orphan in self.sync.settled:
                await self._boot(resume=True, driver=driver)
            else:
                logger.warning("session %s: orphan task %s did not settle after abort",
                               self.session_key, orphan)

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
            await asyncio.sleep(self.heartbeat_s)
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
        while True:
            item = await self._inbound.get()
            try:
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
        elif kind == "event":
            self._on_event(item[1])
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
            if self._channel is not None and self._channel is not ch:
                self._channel.close()
            self._channel = ch
            self._link_up.set()
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
        self._channel = None
        self._link_up.clear()
        if self.vm_state == "parked" or (self.vm_state == "parking" and self._parked_evt.is_set()):
            return                      # the parked guest closed its sockets on purpose
        fut = self._current_future()
        await asyncio.sleep(0.2)      # a settled guest exits right after settle's ack
        if self.sandbox.is_alive() or self._channel is not None:
            return                      # a stream drop: the guest reconnects
        if self.vm_state in ("none", "booting"):
            return                      # stopped on purpose, or a new guest is starting
        if fut is None or self._current_future() is not fut:
            if self.vm_state == "running":
                self.vm_state = "none"  # an idle guest went away (exit at settle)
            return
        # death mid-task
        if self.vm_state == "parking":
            self.vm_state = "running"   # the waiting park() gives up
            self._parked_evt.set()      # ... at once
        if self._resumes >= self.max_resumes:
            msg = ("the guest died again after a resume" if self._resumes
                   else "the guest exited before settling the task")
            fut.set_exception(GuestError(msg))
            return
        self._resumes += 1
        self._spawn(self._death_resume(fut))

    async def _death_resume(self, fut: asyncio.Future) -> None:
        """Restore the base snapshot on the same workspace, hello{resume:
        true}; the guest pulls the log return on :5001 and marks open calls
        lost. max_resumes per task."""
        self.sync.host_event("workflow", {"kind": "resume", "reason": "guest_died",
                                          "resumes": self._resumes,
                                          "acked_seq": self.sync.last_seq})
        try:
            async with self._vm_lock:
                if self._current_future() is not fut:
                    return
                await self._boot(resume=True)
        except Exception as e:
            logger.exception("session %s: resume after death failed", self.session_key)
            if not fut.done():
                fut.set_exception(GuestError(f"resume after the guest died failed: {e}"))

    def _on_event(self, ev: dict) -> None:
        if ev.get("session_id") != self.session_id:
            raise StateSyncError(f"event for foreign session {ev.get('session_id')!r}")
        before = self.sync.last_seq
        if ev.get("type") == "parked" and ev.get("seq") == before + 1:
            # before the ack: once it is acked the guest closes and idle-waits,
            # and must find no listener (sandbox finding)
            self._close_guest_ports()
        try:
            acked = self.sync.accept(ev)
        finally:
            # durable = host ack after append; an accept error acks nothing new
            self._send_raw(ack(self.session_id, ack_seq=self.sync.last_seq))
        if acked == before:
            return                      # duplicate or gap: nothing new
        self._emit_many(self.projector.project(ev))
        etype, body = ev["type"], ev["body"]
        if etype == "tool_call" and self.sides is not None:
            if body["call_id"] in self._lost_calls:
                return
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
            self._parked_evt.set()
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

    def _on_lost(self, call_id: str) -> None:
        """The resumed guest gave up on a host-originated call: stop running
        it; a result that still turns up is dropped (send_control)."""
        self._lost_calls.add(call_id)
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
    async def shutdown_park(self, timeout_s: float) -> bool:
        """Graceful shutdown: park a running guest (also mid-task: side
        calls and questions do not hold a park; a provider call in flight
        finishes first, guest ops get park_grace_s) instead of losing its
        memory with the process, so the next process restores it and adopts
        its open device calls. Waits at most `timeout_s` for `parked` (then
        the snapshot); False when it did not park (close() destroys it, a
        retry resumes from the log)."""
        if self.closed or self.vm_state != "running" or not self._guest_ready():
            return False
        saved, self.park_timeout_s = self.park_timeout_s, min(self.park_timeout_s, timeout_s)
        try:
            return await self.park("shutdown")
        except Exception:
            logger.exception("session %s: park at shutdown failed", self.session_key)
            return False
        finally:
            self.park_timeout_s = saved

    async def close(self) -> None:
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
        for cid, deliver in self._adopted.items():
            self.device_calls.release(cid, deliver)     # a later POST is a 409
        self._adopted.clear()
        try:
            await self._destroy("close")
        except Exception:
            pass
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):
                pass
        if self.proxy is not None:
            await asyncio.to_thread(self.proxy.stop)
        if self.bulk is not None:
            await asyncio.to_thread(self.bulk.stop)
        if self.gateway is not None:
            await asyncio.to_thread(self.gateway.stop)
        if self.gateway_relay is not None:
            self.gateway_relay.close()
        if self.endpoints is not None:
            self.endpoints.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        self._end_all_streams()


def task_ok(t: asyncio.Future) -> bool:
    """A task that finished with a result (not cancelled, no exception)."""
    return t.done() and not t.cancelled() and t.exception() is None


def task_error(t: asyncio.Future | None) -> str | None:
    """The message of a finished task's host failure: its _fail result's
    `host_error`, or the exception it raised; None for a settle."""
    if t is None or not t.done() or t.cancelled():
        return None
    if t.exception() is not None:
        return str(t.exception()) or type(t.exception()).__name__
    return t.result().get("host_error")


def settle_outcome(outcome) -> str:
    """`outcome` when it is a settle outcome (ENUMS), else failed: how both
    drivers normalise a task's outcome (act()'s streams, RunResult)."""
    return outcome if outcome in SETTLE_OUTCOMES else "failed"


def task_outcome(t: asyncio.Future) -> str:
    """A finished task's outcome: its result's, else failed."""
    return settle_outcome(t.result().get("outcome")) if task_ok(t) else "failed"


def end_error(t: asyncio.Future | None, outcome: str) -> str | None:
    """The SSE error that ends an input's stream (finish_input), on either
    driver: none for done, else the host failure or the outcome's name."""
    if outcome == "done":
        return None
    return task_error(t) or f"The task ended: {outcome}."


async def _first(fut: asyncio.Future, evt: asyncio.Event, timeout: float | None = None) -> None:
    """Return when `fut` is done, `evt` is set or `timeout` passed (never cancels `fut`)."""
    waiter = asyncio.ensure_future(evt.wait())
    try:
        await asyncio.wait({fut, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


class Workers:
    """One Worker per running session, by session_key(user, tenant, session_id).

    `sessions` (the DirStore) opens a new Worker's session directory; every
    new Worker gets `worker_kwargs` (its tenant's config it reads itself)
    and a slot of `admission`, the global cap on running tasks
    (max_concurrent, HARNESS_MAX_CONCURRENT_TASKS; None = no cap) that both
    drivers go through (Worker._act)."""

    def __init__(self, sandbox_factory=make_sandbox, *, sessions=None,
                 max_concurrent: int | None = HARNESS_MAX_CONCURRENT_TASKS,
                 **worker_kwargs) -> None:
        self._workers: dict[str, Worker] = {}
        self.sandbox_factory = sandbox_factory
        self.sessions = sessions
        self.admission = Admission(max_concurrent)
        self.worker_kwargs = worker_kwargs

    def get_or_create(self, tenant, session_id: str, registry) -> Worker:
        """The session's live Worker, looked up by key; only a new one opens
        the session directory and reads the tenant's config. A live Worker
        keeps its config until it is closed."""
        key = session_key(tenant.user, tenant.name, session_id)
        w = self._workers.get(key)
        if w is None or w.closed:
            session = self.sessions.get_or_create(tenant.user, tenant.name, session_id)
            w = Worker(session, tenant, registry, self.sandbox_factory(),
                       admission=self.admission, **self.worker_kwargs)
            self._workers[key] = w
        return w

    def get(self, key: str) -> Worker | None:
        return self._workers.get(key)

    def load_parked(self, tenant, registry,
                    want: Callable[[dict], bool] | None = None) -> list[Worker]:
        """A Worker for each of `tenant`'s parked sessions (vm/parked.json;
        with `want`, those whose parked info it accepts) that has none in
        this process yet, as after an agent-api restart: each rebuilds its
        open device calls from its log (Worker._adopt_open_device_calls) and
        its question park. The tool-result route calls it for a call_id it
        does not know. Only sessions whose session.json names exactly this
        user and tenant (DirStore.list_for_tenant), opened where they are:
        never a directory of another tenant, never a new one."""
        loaded = []
        for meta in self.sessions.list_for_tenant(tenant.user, tenant.name):
            sid = meta.get("session_id")
            path = meta["root"] / "vm" / "parked.json"
            if not sid or not path.exists():
                continue
            if want is not None:
                try:
                    if not want(json.loads(path.read_text())):
                        continue
                except (OSError, ValueError):
                    continue
            w = self._workers.get(session_key(tenant.user, tenant.name, sid))
            if w is None or w.closed:
                loaded.append(self.get_or_create(tenant, sid, registry))
        return loaded

    def load_question_parks(self, tenants, registry) -> list[Worker]:
        """At startup: a Worker for every session parked on a question of
        the direct path, so its answer wait goes on (a restored driver,
        Worker._drive_restored) without waiting for a request to load it."""
        loaded = []
        for tenant in tenants:
            loaded += self.load_parked(tenant, registry, want=lambda info: bool(
                info.get("question")) and info.get("driver") == DIRECT)
        return loaded

    async def close(self, key: str) -> None:
        w = self._workers.pop(key, None)
        if w is not None:
            await w.close()

    async def shutdown(self, park_s: float = HARNESS_SHUTDOWN_PARK_S) -> None:
        """Every Worker closed; first (park_s > 0) every running guest is
        parked, concurrently (Worker.shutdown_park), so a restart adopts it."""
        if park_s > 0:
            workers = [w for w in self._workers.values() if not w.closed]
            await asyncio.gather(*(w.shutdown_park(park_s) for w in workers),
                                 return_exceptions=True)
        for key in list(self._workers):
            await self.close(key)
