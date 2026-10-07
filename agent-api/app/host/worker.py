"""worker — the host half of one session (PLAN.md 3, 4, 8; M1: direct call,
Temporal activities in M5).

  Worker(session, tenant, registry, sandbox, sinks=None)
    .act(task_input) -> settle body     task_input = {input_id, text, kind?,
                                        attachments?, answer_to?, ticket_id?}
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

Threads touch sockets only (endpoints.py); everything reaches the asyncio
loop through loop.call_soon_threadsafe and one inbound asyncio.Queue that a
pump task drains. The pump is the single writer of state_sync.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid

from protocol.allowlist import effective_tools
from protocol.messages import Outbox, ProtocolError, ack, control_message

from app.config import (
    HARNESS_GUEST_ROOT, HARNESS_HEARTBEAT_S, HARNESS_HELLO_TIMEOUT_S,
    HARNESS_HOST_TOOL_TIMEOUT_S, HARNESS_MAX_TURNS, HARNESS_PORTS,
    HARNESS_QUESTION_TIMEOUT_S, HARNESS_SANDBOX, HARNESS_SPILL_THRESHOLD,
    HARNESS_STOP_AT_SETTLE, HARNESS_WALL_CLOCK_S, LLM_ENABLE_THINKING,
    LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE, LLM_URL, MAX_TOOLS,
    SYSTEM_PROMPT, TOOL_TIMEOUT,
)
from app.host.endpoints import ControlChannel, Endpoints
from app.host.llm_proxy import LLMProxy
from app.host.sandbox import Sandbox
from app.host.sides import DEVICE_CALLS, DeviceCalls, Sides
from app.host.state_sync import StateSync, StateSyncError
from app.host.stream import DONE, Projector, sse, tokens
from app.user.context_builder import ContextBuilder

logger = logging.getLogger("host.worker")

HARNESS_VERSION = "0.0.1"


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
                 stop_at_settle: bool = HARNESS_STOP_AT_SETTLE) -> None:
        self.session = session
        self.tenant = tenant
        self.registry = registry
        self.sandbox = sandbox
        self.sinks: list[asyncio.Queue] = list(sinks or [])
        self.device_calls = device_calls
        self.llm_url = llm_url
        self.ports = dict(ports)
        self.hello_timeout_s = hello_timeout_s
        self.heartbeat_s = heartbeat_s
        self.wall_clock_s = wall_clock_s
        self.stop_at_settle = stop_at_settle

        meta = session.read_meta()
        self.session_id: str = meta.get("session_id") or session.root.name
        self.user: str = tenant.user
        self.session_key = f"{tenant.user}/{tenant.name}_{self.session_id}"
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
        self._hello_run = 0          # guest run number hello was sent for
        self._guest_run = 0          # increments on every sandbox start
        self._stop_accept = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._task_lock = asyncio.Lock()
        self._current: tuple[str, asyncio.Future] | None = None
        self.last_hello: dict | None = None
        self.closed = False

    # ------------------------------------------------------------ sinks
    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.sinks.append(q)
        for call in self.device_calls.open_for(self.session_key):
            # PLAN.md 8: an open device call is re-emitted to the stream that attaches
            q.put_nowait(sse("tool_call", call))
        return q

    def detach(self, q: asyncio.Queue) -> None:
        try:
            self.sinks.remove(q)
        except ValueError:
            pass

    def _emit(self, item) -> None:
        for q in list(self.sinks):
            q.put_nowait(item)

    def _emit_many(self, items) -> None:
        for s in items:
            self._emit(s)

    def _end_stream(self) -> None:
        self._emit(None)

    # ------------------------------------------------------------ hello
    def tool_entries(self) -> list[dict]:
        """Host/device tools for hello.config.tools: the tenant's allowlist
        applied to the registry, each host skill with its OpenAI schema
        (keyword prefilter retired, D16). Guest built-ins are implicit."""
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
        """Recalled memory bodies for the input's preamble (D23)."""
        cb = ContextBuilder(user_id=self.user, session=self.session,
                            base_prompt=self.tenant.system_prompt)
        recalled = cb.user_memory.recall(text) + cb.session_memory.recall(text)
        return [{"kind": "memory", "type": r.type, "slug": r.slug, "name": r.name,
                 "body": r.body.strip()} for r in recalled[:3]]

    def limits(self) -> dict:
        return {
            "wall_clock_s": int(self.wall_clock_s),
            "max_tools": self.tenant.max_tools or MAX_TOOLS,
            "max_turns": HARNESS_MAX_TURNS,
            "max_tokens": self.tenant.max_tokens or LLM_MAX_TOKENS,
            "op_timeout_s": {"guest": 120, "host": HARNESS_HOST_TOOL_TIMEOUT_S,
                             "device": TOOL_TIMEOUT, "mcp": 300},
            "spill_threshold": HARNESS_SPILL_THRESHOLD,
        }

    def build_hello(self, resume: bool = False) -> dict:
        params: dict = {"max_tokens": self.tenant.max_tokens or LLM_MAX_TOKENS,
                        "temperature": LLM_TEMPERATURE}
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
    def send_control(self, type: str, body: dict) -> dict:
        """Mint the next cseq, keep the message until acked, send it if the
        link is up (else it goes with the resend on the next link)."""
        assert self._outbox is not None
        msg = control_message(self.session_id, self._outbox.next_number(), type, body)
        self._outbox.push(msg)
        if self._channel is not None and not self._channel.closed:
            self._channel.send(msg)
        return msg

    def _send_raw(self, msg: dict) -> None:
        if self._channel is not None and not self._channel.closed:
            self._channel.send(msg)

    # ------------------------------------------------------------ plumbing
    def _ensure_plumbing(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._inbound = asyncio.Queue()
            self._link_up = asyncio.Event()
        if self.endpoints is None:
            self.endpoints = Endpoints(self.session.root, self.ports)
            self.proxy = LLMProxy(self.endpoints.listener(self.ports["llm"]), self.llm_url,
                                  self.sync.host_event)
            self.proxy.start()
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

    def _accept_loop(self) -> None:
        n = 0
        while not self._stop_accept.is_set():
            conn = self.endpoints.accept_control(timeout=0.5) if self.endpoints else None
            if conn is None:
                continue
            n += 1
            ch = ControlChannel(conn, self._loop, self._inbound, name=f":{n}")
            self._loop.call_soon_threadsafe(self._inbound.put_nowait, ("link", "up", ch))

    async def _start_guest(self) -> None:
        """Start a fresh guest run (new log run), wait for its :5000
        connection, send hello."""
        self.sync.begin_run()
        self._outbox = Outbox(self.session_id, "cseq")   # a fresh guest's inbox starts at 0
        self._link_up.clear()
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._guest_run += 1
        self.sync.host_event("vm", {"kind": "start", "run": self._guest_run,
                                    "sandbox": type(self.sandbox).__name__})
        await asyncio.to_thread(self.sandbox.start, self.session.root)
        try:
            await asyncio.wait_for(self._link_up.wait(), timeout=self.hello_timeout_s)
        except asyncio.TimeoutError:
            await self._stop_guest("no connection")
            raise GuestError(f"guest did not connect within {self.hello_timeout_s}s")
        hello = self.build_hello(resume=False)
        self.last_hello = hello
        self.send_control("hello", hello)
        self._hello_run = self._guest_run

    async def _stop_guest(self, reason: str = "settle") -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._link_up.clear()
        await asyncio.to_thread(self.sandbox.stop)
        self.sync.host_event("vm", {"kind": "stop", "run": self._guest_run, "reason": reason})

    def _guest_ready(self) -> bool:
        return (self.sandbox.is_alive() and self._channel is not None
                and not self._channel.closed and self._hello_run == self._guest_run)

    # ------------------------------------------------------------ act
    async def act(self, task_input: dict) -> dict:
        if self.closed:
            raise GuestError("worker closed")
        input_id = task_input["input_id"]
        self._ensure_plumbing()

        cur = self._current
        if cur is not None and cur[0] == input_id and not cur[1].done():
            # redelivered while running: send it anyway (the guest dedupes), attach
            if self._guest_ready():
                self.send_control(*self._task_message(task_input))
            return await asyncio.shield(cur[1])

        async with self._task_lock:
            if input_id in self.sync.settled:
                return self._replay_settled(input_id)
            fut = self._loop.create_future()
            self._current = (input_id, fut)
            try:
                result = await self._run(task_input, fut)
            except BaseException as e:
                if not fut.done():
                    fut.set_exception(e)
                raise
            if not fut.done():
                fut.set_result(result)
            return result

    def _task_message(self, task_input: dict) -> tuple[str, dict]:
        kind = task_input.get("kind", "task")
        if task_input.get("answer_to"):
            return "ui_response", {"input_id": task_input["input_id"],
                                   "question_id": task_input["answer_to"],
                                   "answer": task_input.get("text", "")}
        if kind not in ("task", "follow_up", "steer"):
            kind = "task"
        attachments = task_input.get("attachments")
        if attachments is None:
            attachments = self.attachments(task_input.get("text", ""))
        return kind, {"input_id": task_input["input_id"], "text": task_input.get("text", ""),
                      "attachments": attachments}

    def _replay_settled(self, input_id: str) -> dict:
        body = self.sync.settled[input_id]
        self.sync.host_event("workflow", {"kind": "task_signal", "input_id": input_id,
                                          "dedupe": "settled"})
        if body.get("answer"):
            self._emit_many(tokens(body["answer"].strip()))
        self._emit(DONE)
        self._end_stream()
        return body

    async def _run(self, task_input: dict, fut: asyncio.Future) -> dict:
        input_id = task_input["input_id"]
        self.sync.ticket_id = task_input.get("ticket_id")
        self.sync.host_event("workflow", {"kind": "task_signal", "input_id": input_id})
        try:
            if not self._guest_ready():
                await self._start_guest()
            self.send_control(*self._task_message(task_input))
            hb = self._loop.create_task(self._heartbeat())
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout=self.wall_clock_s + 5)
            except asyncio.TimeoutError:
                result = self._fail(input_id, "timeout",
                                    f"The task exceeded its wall clock of {int(self.wall_clock_s)}s.")
            finally:
                hb.cancel()
        except GuestError as e:
            result = self._fail(input_id, "failed", f"The agent sandbox failed: {e}")
        except Exception as e:
            logger.exception("task %s failed", input_id)
            result = self._fail(input_id, "failed", f"The agent failed: {e}")
        self.sync.host_event("workflow", {"kind": "completed", "input_id": input_id,
                                          "outcome": result.get("outcome")})
        if self.stop_at_settle or not self.sandbox.is_alive():
            await self._stop_guest("settle" if result.get("outcome") == "done" else result.get("outcome", "failed"))
        if self.sides is not None:
            await self.sides.cancel_all()
        self._end_stream()
        self.sync.ticket_id = None
        return result

    def _fail(self, input_id: str, outcome: str, message: str) -> dict:
        logger.error("task %s: %s (%s)", input_id, message, outcome)
        self._emit_many(tokens(message))
        self._emit(sse("error", {"class": "fatal", "message": message}))
        self._emit(DONE)
        try:
            self.session.append_history("assistant", message, ticket_id=self.sync.ticket_id)
            self.session.append_trace("llm_error", {"error": message[:500], "outcome": outcome},
                                      ticket_id=self.sync.ticket_id)
        except Exception:
            pass
        return {"input_id": input_id, "outcome": outcome, "answer": message,
                "usage": {}, "manifest": {}}

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            if self._guest_ready():
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
        fut = self._current_future()
        if fut is None:
            return
        await asyncio.sleep(0.2)      # the guest exits right after settle's ack
        if not self.sandbox.is_alive() and self._current_future() is fut:
            fut.set_exception(GuestError("the guest exited before settling the task"))

    def _on_event(self, ev: dict) -> None:
        if ev.get("session_id") != self.session_id:
            raise StateSyncError(f"event for foreign session {ev.get('session_id')!r}")
        before = self.sync.last_seq
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
            self.sides.handle_tool_call(ev)
        elif etype == "settle":
            fut = self._current_future()
            if fut is not None and body["input_id"] == self._current[0]:
                fut.set_result(body)
        elif etype == "error" and body["class"] == "fatal":
            fut = self._current_future()
            if fut is not None:
                fut.set_exception(GuestError(body["message"]))

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
        self._stop_accept.set()
        fut = self._current_future()
        if fut is not None:
            fut.set_exception(GuestError("worker closed"))
        if self.sides is not None:
            await self.sides.cancel_all()
        try:
            await self._stop_guest("close")
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
        if self.endpoints is not None:
            self.endpoints.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        self._end_stream()


class Workers:
    """One Worker per running session (keyed like SessionQueue)."""

    def __init__(self, sandbox_factory=make_sandbox, **worker_kwargs) -> None:
        self._workers: dict[str, Worker] = {}
        self.sandbox_factory = sandbox_factory
        self.worker_kwargs = worker_kwargs

    def get_or_create(self, session, tenant, registry, **kwargs) -> Worker:
        key = f"{tenant.user}/{tenant.name}_{session.read_meta().get('session_id', session.root.name)}"
        w = self._workers.get(key)
        if w is None or w.closed:
            w = Worker(session, tenant, registry, self.sandbox_factory(),
                       **{**self.worker_kwargs, **kwargs})
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
