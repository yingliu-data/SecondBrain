"""worker — the host half of one session (PLAN.md 3, 4, 8; M1: direct call,
Temporal activities in M5).

  Worker(session, tenant, registry, sandbox, sinks=None)
    .act(task_input) -> settle body     task_input = {input_id, text, kind?,
                                        attachments?, ticket_id?}
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

M3 (harness/docs/M3-CONTRACTS.md): one long-lived guest per session.
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

M5 (harness/docs/M5-CONTRACTS.md): two drivers over one task API. The
Temporal activities (activities.py) are one; act() is the other, in-process
(the direct path). `answer_wait_s` (construction config, Workers.config_for)
set = the workflow path: hello limits carry it, the guest parks itself on a question deadline
(parked{reason: question, question_id}), the worker adopts the park and
records `question_park` on the parked state; the task's wall clock stops
while it waits.
  .ensure_ready()               boot / wake the guest (prepare_vm)
  .start_task(task_input)       the task of an input, worker-owned (a retry
                                re-attaches to it by input_id); task_input may
                                carry its own wall_clock_s
  .wait_outcome(input_id)       its settle (or the host's failure result), or
                                {outcome: parked, question_id} at a question park
  .on_heartbeat(input_id, beat) beat() on every heartbeat tick of that task
  .answer(question_id, text, input_id)   ui_response to the live/parked guest
  .abort(reason)                abort{reason} to the live/parked guest
  .finish_input(input_id, error)  the ONLY end of an input's streams: error
                                (if any) + done + end of stream; act() calls it
                                when its task returns, the workflow at deliver
  .replay(input_id, sink=None)  a settled input's answer again, then its end
                                (its settle body; None when not settled)

Threads touch sockets only (endpoints.py); everything reaches the asyncio
loop through loop.call_soon_threadsafe and one inbound asyncio.Queue that a
pump task drains. The pump is the single writer of state_sync.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, NamedTuple

from protocol.allowlist import effective_tools
from protocol.messages import Outbox, ProtocolError, ack, control_message

from app.config import (
    HARNESS_CONTEXT_WINDOW_TOKENS, HARNESS_GUEST_ROOT, HARNESS_HEARTBEAT_S,
    HARNESS_HELLO_TIMEOUT_S, HARNESS_HOST_TOOL_TIMEOUT_S, HARNESS_MAX_RESUMES,
    HARNESS_MAX_TURNS, HARNESS_PARK_GRACE_S, HARNESS_PARK_TIMEOUT_S, HARNESS_PORTS, HARNESS_QUESTION_TIMEOUT_S,
    HARNESS_RESERVE_TOKENS, HARNESS_SANDBOX, HARNESS_SESSION_IDLE_S,
    HARNESS_SPILL_THRESHOLD, HARNESS_STOP_AT_SETTLE,
    HARNESS_TOKEN_BUDGET, HARNESS_WALL_CLOCK_S, LLM_ENABLE_THINKING,
    LLM_MAX_TOKENS, LLM_MODEL, LLM_STREAM, LLM_TEMPERATURE, LLM_URL, MAX_TOOLS,
    MCP_GATEWAY_TOOLS_TIMEOUT_S, MCP_GATEWAY_URL, SYSTEM_PROMPT, TOOL_TIMEOUT,
)
from app.host.bulk_server import BulkServer
from app.gateway.client import GatewayError, fetch_session_tools
from app.host.endpoints import ControlChannel, Endpoints
from app.host.gateway_forwarder import GatewayForwarder, GatewayRelay
from app.host.llm_proxy import LLMProxy
from app.host.sandbox import Sandbox, SandboxError
from app.host.sides import DEVICE_CALLS, DeviceCalls, Sides
from app.host.state_sync import StateSync, StateSyncError
from app.host.stream import DONE, Projector, fatal_items, sse, tokens
from app.session.ids import session_key
from app.user.context_builder import ContextBuilder
from app.util.ipc import atomic_write_json

logger = logging.getLogger("host.worker")

HARNESS_VERSION = "0.0.3"


class ControlPolicy(NamedTuple):
    """How one control type is handled outside a plain send."""
    while_parked: str       # "hold": kept unnumbered, the guest wakes for it; "drop"
    carried: bool           # unacked at a death resume: numbered again after the new hello
    wake_order: int         # held controls are sent at the wake lowest first


# M3-CONTRACTS §3.5: the wake sends held controls `ui_response` first
CONTROL_POLICY: dict[str, ControlPolicy] = {
    "hello": ControlPolicy("hold", False, 1),
    "heartbeat": ControlPolicy("drop", False, 1),
    "park": ControlPolicy("drop", False, 1),
    "ui_response": ControlPolicy("hold", True, 0),
    "task": ControlPolicy("hold", True, 1),
    "follow_up": ControlPolicy("hold", True, 1),
    "steer": ControlPolicy("hold", True, 1),
    "side_result": ControlPolicy("hold", True, 1),
    "abort": ControlPolicy("hold", True, 1),
}
_DEFAULT_POLICY = ControlPolicy("hold", True, 1)


def control_policy(type: str) -> ControlPolicy:
    return CONTROL_POLICY.get(type, _DEFAULT_POLICY)


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
    preamble (PLAN.md 7)."""
    out = []
    for line in prompt.splitlines():
        had = bool(line.strip())
        line = _TIME_FRAGMENT.sub("", line).replace("{current_time}", "").rstrip()
        if had and not line.strip():
            continue
        out.append(line)
    return "\n".join(out)


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
                 answer_wait_s: int | None = None) -> None:
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

        meta = session.read_meta()
        self.session_id: str = meta.get("session_id") or session.root.name
        self.user: str = tenant.user
        self.session_key = session_key(tenant.user, tenant.name, self.session_id)
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
        self._current: tuple[str, asyncio.Future] | None = None  # input_id, its settle
        # the running task's wall clock (its input's, else wall_clock_s): hello limits
        self.task_wall_clock_s: float = wall_clock_s
        self.last_hello: dict | None = None
        self.closed = False
        # M3: park, resume, idle, death
        self.bulk: BulkServer | None = None
        # M4: the :5011 forwarder (per listener) and its worker-lived relay
        # (held calls survive the listener closing around a park, §3.3)
        self.gateway: GatewayForwarder | None = None
        self.gateway_relay: GatewayRelay | None = None
        # the session's gateway entries from the first successful fetch:
        # pins are fixed per session, a resume hello reuses them
        self.gateway_entries: list[dict] | None = None
        # M5: the workflow path (None = direct path, M1-M4 behaviour unchanged)
        self.answer_wait_s = answer_wait_s
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
        self._load_parked()

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
        for call in self.device_calls.open_for(self.session_key):
            # PLAN.md 8: an open device call is re-emitted to the stream that attaches
            q.put_nowait(sse("tool_call", call))
        current = self.current_input_id
        if input_id is None or current is None or input_id == current:
            # M5 §3: so is an open question (the phone gets it on reconnect);
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

    # ------------------------------------------------------------ M5: the task API
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

    async def ensure_ready(self) -> None:
        """A guest that can take a control: wake a parked one, boot one
        that is not running (prepare_vm; also the start of every task)."""
        self._ensure_plumbing()
        async with self._vm_lock:        # waits out a park in progress
            if self.vm_state == "parked":
                await self._wake_locked()
            elif not self._guest_ready():
                await self._boot(resume=False)

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
        asyncio.TimeoutError once it (+5 s) ran out; the clock stops while
        the task is parked on a question (M5, D12: the workflow's answer
        timer bounds that wait)."""
        left = None if wall_clock_s is None else wall_clock_s + 5
        while True:
            evt, qp = self._changed, self.question_park
            if qp is not None and input_id is not None and qp["input_id"] == input_id:
                return {"input_id": input_id, "outcome": "parked",
                        "question_id": qp["question_id"]}
            if aw.done():
                return aw.result()
            if qp is not None or left is None:
                await _first(aw, evt)
                continue
            t0 = time.monotonic()
            await _first(aw, evt, timeout=left)
            left -= time.monotonic() - t0
            if not aw.done() and left <= 0 and self.question_park is None:
                raise asyncio.TimeoutError

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

    def answer(self, question_id: str, text: str, input_id: str) -> str:
        """ui_response{question_id, answer} to the guest (a parked one is
        restored first, ui_response first); send_control's status."""
        self._ensure_plumbing()
        if self.question_park is not None and self.question_park["question_id"] == question_id:
            self.question_park = None
        status = self.send_control("ui_response", {"input_id": input_id,
                                                   "question_id": question_id, "answer": text})
        self.sync.host_event("workflow", {"kind": "answer_signal", "input_id": input_id,
                                          "question_id": question_id, "sent": status,
                                          "vm_state": self.vm_state})
        return status

    def abort(self, reason: str) -> str:
        """abort{reason}: the guest settles the running task itself (M5 §4);
        send_control's status."""
        self._ensure_plumbing()
        status = self.send_control("abort", {"reason": reason})
        if status != "no_guest":
            self.sync.host_event("workflow", {"kind": "abort", "reason": reason,
                                              "input_id": self.current_input_id})
        return status

    def finish_input(self, input_id: str, error: str | None = None, **extra) -> None:
        """The one terminal step of an input's streams: `error` (if any, with
        `extra`) + `done` + end of stream to its sinks; its task is
        forgotten. act() calls it when its task returns, the workflow path
        at deliver."""
        self._emit_many(fatal_items(error, **extra) if error else [DONE], input_id)
        self._end_stream(input_id)
        self._acts.pop(input_id, None)

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
        entries verbatim (M4-CONTRACTS §4, fetched by load_gateway_entries)
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
        harness-engine skills, each with its OpenAI schema (keyword prefilter
        retired, D16)."""
        allowed = self.tenant.allowed_skill_names()
        server = self.registry.get_server_tool_names(allowed, engine="harness")
        device = self.registry.get_device_tool_names(allowed, engine="harness")
        entries: list[dict] = []
        seen: set[str] = set()
        for d in self.registry.get_active_tool_definitions(allowed, engine="harness"):
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
        """Recalled memory bodies for the input's preamble (D23)."""
        cb = ContextBuilder(user_id=self.user, session=self.session,
                            base_prompt=self.tenant.system_prompt)
        recalled = cb.user_memory.recall(text) + cb.session_memory.recall(text)
        return [{"kind": "memory", "type": r.type, "slug": r.slug, "name": r.name,
                 "body": r.body.strip()} for r in recalled[:3]]

    def limits(self) -> dict:
        return {
            "wall_clock_s": int(self.task_wall_clock_s),
            "max_tools": self.tenant.max_tools or MAX_TOOLS,
            "max_turns": HARNESS_MAX_TURNS,
            "max_tokens": self.tenant.max_tokens or LLM_MAX_TOKENS,
            "op_timeout_s": {"guest": 120, "host": HARNESS_HOST_TOOL_TIMEOUT_S,
                             "device": TOOL_TIMEOUT, "mcp": 300},
            "spill_threshold": HARNESS_SPILL_THRESHOLD,
            # M2: the guest compacts when its estimate exceeds window - reserve
            # (the token budget is a proxy knob, not a guest limit)
            "context_window_tokens": HARNESS_CONTEXT_WINDOW_TOKENS,
            "reserve_tokens": HARNESS_RESERVE_TOKENS,
            # M3: running guest ops get this long after `park`, then killed
            "park_grace_s": self.park_grace_s,
            # M5: > 0 = a question deadline parks the task (workflow path only)
            **({"answer_wait_s": int(self.answer_wait_s)} if self.answer_wait_s is not None else {}),
        }

    def build_hello(self, resume: bool = False) -> dict:
        params: dict = {"max_tokens": self.tenant.max_tokens or LLM_MAX_TOKENS,
                        "temperature": LLM_TEMPERATURE,
                        "stream": LLM_STREAM}      # D17: false = one call per turn
        if not LLM_ENABLE_THINKING:
            params["chat_template_kwargs"] = {"enable_thinking": False}
        config = {
            "model": LLM_MODEL,
            "params": params,
            "tools": self.tool_entries(),
            "limits": self.limits(),
            "hooks": {},
            "async_tools": False,
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
        """The one way a control reaches the guest (CONTROL_POLICY). Returns
          "sent"      numbered (next cseq) and kept until acked; sent now if
                      the link is up, else with the resend on the next link
                      (a stream drop between restore and reconnect is not a
                      lost guest). Also a `side_result` for a call the guest
                      declared lost: dropped, logged `input{dedupe: late}`
                      (PLAN.md 5)
          "held"      the guest is parked: a `hold` control is kept
                      unnumbered, the guest wakes (restore) and the wake
                      numbers the held controls by wake_order; a `drop` one
                      (heartbeat, park) is dropped
          "no_guest"  no guest run can take it (never booted, or gone)"""
        if type == "side_result" and body.get("call_id") in self._lost_calls:
            self.sync.host_event("input", {"kind": "side_result", "call_id": body.get("call_id"),
                                           "side": body.get("side"), "dedupe": "late"})
            return "sent"
        if self.vm_state == "parked":
            if control_policy(type).while_parked == "hold":
                self._deferred.append((type, body))
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
        """Number and send held controls by wake_order (`ui_response` first,
        M3-CONTRACTS §3.5)."""
        for t, b in sorted(items, key=lambda tb: control_policy(tb[0]).wake_order):
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
                               session_key=self.session_key)
        if self._pump is None or self._pump.done():
            self._pump = loop.create_task(self._pump_loop(), name=f"pump:{self.session_key}")

    def _on_bulk_put(self, kind: str, path: str, nbytes: int, sha256: str) -> None:
        self.sync.host_event("bulk", {"kind": kind, "path": path, "bytes": nbytes, "sha256": sha256})

    def _accept_loop(self) -> None:
        n = 0
        while not self._stop_accept.is_set():
            conn = self.endpoints.accept_control(timeout=0.5) if self.endpoints else None
            if conn is None:
                continue
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
        finding). Held gateway calls keep running in the relay (§3.3)."""
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

    async def _boot(self, *, resume: bool) -> None:
        """Start a guest run and send hello. Fresh (resume=False): a new log
        run, cseq from 1. Resume (death, or a park whose snapshot is gone):
        cseq continues, `hello{resume: true}`, the guest takes the log return
        on :5001 and continues the run's seq; the unacked controls of the
        previous guest are numbered again after the hello. Base snapshot
        restore when the sandbox has one, else a rootfs boot (start)."""
        carried: list[tuple[str, dict]] = []
        if resume:
            highest = 0
            if self._outbox is not None:
                carried = [(m["type"], m["body"]) for m in self._outbox.pending
                           if control_policy(m["type"]).carried]
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
        """Park the guest (M3-CONTRACTS §3): send `park`, wait for the acked
        `parked`, snapshot the VM to {session}/vm/snapshot. True when parked.
        The next act()/ui_response/side_result restores it.

        idle=True (the idle timer, D18): only while no task runs, and the VM
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
        """The guest parked on its own (M5 §4: a question deadline with
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
        async with self._vm_lock:
            await self._wake_locked()

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
        """session_idle_s after a settle: park + snapshot + destroy (D18)."""
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
        guest: cseq continues. Whether the session snapshot is still usable
        is the sandbox's to say (restore() refuses one that is not)."""
        try:
            atomic_write_json(self.parked_file, {
                "cseq": self._outbox.highest_sent if self._outbox is not None else 0,
                "reason": reason, "checkpoint_seq": checkpoint_seq,
                "last_seq": self.sync.last_seq, "at": now_ms()})
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

    # ------------------------------------------------------------ act
    async def act(self, task_input: dict) -> dict:
        """The direct path: an in-process driver over the task API (the
        workflow's activities are the other one): start_task, its result,
        finish_input. A redelivered input (an input_id already seen) is
        sent again anyway - the guest dedupes - and shares the running
        task; one that already settled gets its answer replayed, no guest."""
        input_id = task_input["input_id"]
        task = self._acts.get(input_id)
        if task is not None and not task.done():
            if self.current_input_id == input_id and (self.vm_state == "parked"
                                                      or self._guest_ready()):
                self.send_control(*self._task_message(task_input))
            return await asyncio.shield(task)
        body = self.replay(input_id)      # settled: its answer again, its streams end
        if body is not None:
            return self._settled_again(input_id, body)
        task = self.start_task(task_input)
        # before shield's own callback: the streams end before act() returns
        task.add_done_callback(lambda t: self.finish_input(input_id, task_error(t)))
        return await asyncio.shield(task)

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
        try:
            self._ensure_plumbing()
        except Exception as e:      # e.g. the unix socket path is too long
            logger.exception("session %s: host plumbing failed", self.session_key)
            message = f"The agent sandbox failed: {e}"
            self._fail(input_id, "failed", message)
            raise GuestError(message) from e
        async with self._task_lock:
            body = self.sync.settled.get(input_id)
            if body is not None:
                return self._settled_again(input_id, body)
            fut = self._loop.create_future()
            self._current = (input_id, fut)
            self.task_wall_clock_s = task_input.get("wall_clock_s") or self.wall_clock_s
            try:
                result = await self._run(task_input, fut)
            except BaseException as e:
                if not fut.done():
                    fut.set_exception(e)
                raise
            finally:
                self.task_wall_clock_s = self.wall_clock_s
            if not fut.done():
                fut.set_result(result)
            return result

    def _task_message(self, task_input: dict) -> tuple[str, dict]:
        kind = task_input.get("kind", "task")
        if kind not in ("task", "follow_up", "steer"):
            kind = "task"
        attachments = task_input.get("attachments")
        if attachments is None:
            attachments = self.attachments(task_input.get("text", ""))
        return kind, {"input_id": task_input["input_id"], "text": task_input.get("text", ""),
                      "attachments": attachments}

    def _settled_again(self, input_id: str, body: dict) -> dict:
        """`body`, the stored settle of an input seen again (logged)."""
        self.sync.host_event("workflow", {"kind": "task_signal", "input_id": input_id,
                                          "dedupe": "settled"})
        return body

    async def _run(self, task_input: dict, fut: asyncio.Future) -> dict:
        input_id = task_input["input_id"]
        wall_clock_s = self.task_wall_clock_s
        self.sync.ticket_id = task_input.get("ticket_id")
        self.sync.host_event("workflow", {"kind": "task_signal", "input_id": input_id})
        self._cancel_idle()
        self._resumes = 0
        guest_settled = False
        try:
            await self.ensure_ready()
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
        self.sync.ticket_id = None
        if self.vm_state == "running" and not self.closed:
            self._arm_idle()
        return result

    def _fail(self, input_id: str, outcome: str, message: str) -> dict:
        """The host's failure result of a task: its message as the answer
        (streamed, stored in history) and `host_error` (finish_input's error)."""
        logger.error("task %s: %s (%s)", input_id, message, outcome)
        self._emit_many(tokens(message), input_id)
        try:
            self.session.append_history("assistant", message, ticket_id=self.sync.ticket_id)
            self.session.append_trace("llm_error", {"error": message[:500], "outcome": outcome},
                                      ticket_id=self.sync.ticket_id)
        except Exception:
            pass
        return {"input_id": input_id, "outcome": outcome, "answer": message,
                "usage": {}, "manifest": {}, "host_error": message}

    async def _heartbeat(self, input_id: str) -> None:
        """Every heartbeat_s while the task of `input_id` lives: its
        on_heartbeat listeners (the run_task activity's Temporal heartbeat,
        also while the task is queued); while it runs, the guest heartbeat
        (a parked guest gets none, the host event says parked; none at all
        while parked on a question: the workflow holds that task)."""
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
            # PROTOCOL.md 4: the host answers with its ack of seq, then resends
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
        if fut is None or self._current_future() is not fut:
            if self.vm_state == "running":
                self.vm_state = "none"  # an idle guest went away (exit at settle)
            return
        # death mid-task (M3-CONTRACTS §4)
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
            # and must find no listener (M3-CONTRACTS §3, sandbox finding)
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
    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._cancel_idle()
        self._stop_accept.set()
        fut = self._current_future()
        if fut is not None:
            fut.set_exception(GuestError("worker closed"))
        if self._wake_task is not None and not self._wake_task.done():
            self._wake_task.cancel()
        if self.sides is not None:
            await self.sides.cancel_all()
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


async def _first(fut: asyncio.Future, evt: asyncio.Event, timeout: float | None = None) -> None:
    """Return when `fut` is done, `evt` is set or `timeout` passed (never cancels `fut`)."""
    waiter = asyncio.ensure_future(evt.wait())
    try:
        await asyncio.wait({fut, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


class Workers:
    """One Worker per running session, by session_key(user, tenant, session_id).

    `sessions` (the DirStore) opens a new Worker's session directory;
    `config_for(tenant)` gives a new Worker's construction config on top of
    `worker_kwargs` (main.py: the workflow path's answer_wait_s while
    Temporal runs)."""

    def __init__(self, sandbox_factory=make_sandbox, *, sessions=None,
                 config_for: Callable[[object], dict] | None = None, **worker_kwargs) -> None:
        self._workers: dict[str, Worker] = {}
        self.sandbox_factory = sandbox_factory
        self.sessions = sessions
        self.config_for = config_for or (lambda tenant: {})
        self.worker_kwargs = worker_kwargs

    def get_or_create(self, tenant, session_id: str, registry) -> Worker:
        """The session's live Worker, looked up by key; only a new one opens
        the session directory and reads config_for(tenant). A live Worker
        keeps its config until it is closed."""
        key = session_key(tenant.user, tenant.name, session_id)
        w = self._workers.get(key)
        if w is None or w.closed:
            session = self.sessions.get_or_create(tenant.user, tenant.name, session_id)
            w = Worker(session, tenant, registry, self.sandbox_factory(),
                       **{**self.worker_kwargs, **self.config_for(tenant)})
            self._workers[key] = w
        return w

    def get(self, key: str) -> Worker | None:
        return self._workers.get(key)

    async def close(self, key: str) -> None:
        w = self._workers.pop(key, None)
        if w is not None:
            await w.close()

    async def shutdown(self) -> None:
        for key in list(self._workers):
            await self.close(key)
