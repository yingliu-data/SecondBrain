"""vm — the guest VM's lifecycle half of a Worker (docs/HOST.md: one
long-lived guest per session).

  vm_state      none -> booting -> running -> parking -> parked -> booting ...
  boot          _boot: a fresh guest (base snapshot restore, else a rootfs
                start) + hello; resume=True: hello{resume: true} + the log return
  park          park(reason, timeout_s=): control `park`, wait for the acked
                `parked`, snapshot to {session}/vm/snapshot, vm/parked.json
  wake          _wake_locked: restore the session snapshot (no hello), send
                the held controls ui_response first; fallback: a resume boot
  idle          session_idle_s after a settle: park + snapshot + destroy

VMLifecycle is a mixin of Worker: its methods use the Worker's sandbox,
sync, outbox, ports and wait primitives (_pulse / _until). Split out of
worker.py for size; it is not used on its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from protocol.allowlist import effective_tools
from protocol.clock import elapsed_ms, now_ms
from protocol.fsio import atomic_write
from protocol.messages import Outbox

from app.host.bulk_server import BulkServer
from app.host.gateway_forwarder import GatewayForwarder
from app.host.outcomes import GuestError
from app.host.sandbox import SandboxError

logger = logging.getLogger("host.worker")

# unacked at a death resume: NOT numbered again after the new hello
NOT_CARRIED = frozenset({"hello", "heartbeat", "park"})


def write_json(path: Path, obj: dict) -> None:
    """An atomic, durable JSON file (a ticket, vm/parked.json): what a
    restart reads back. 0644 as before."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(obj, separators=(",", ":")).encode("utf-8"), mode=0o644)


def wake_order(item: tuple[str, dict]) -> bool:
    """Sort key of the held controls a wake sends: `ui_response` first,
    the rest in the order they were held."""
    return item[0] != "ui_response"


class VMLifecycle:
    # ------------------------------------------------------------ state
    @property
    def vm_state(self) -> str:
        return self._vm_state

    @vm_state.setter
    def vm_state(self, value: str) -> None:
        """none | booting | running | parking | parked. `question_park` is a
        field of the parked state: any other state clears it. Every change
        wakes the waiters (_until)."""
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
        question; a change wakes the waiters (_until)."""
        if value == self._question_park:
            return
        self._question_park = value
        self._pulse()

    def _set_link(self, up: bool) -> None:
        if up != self._link_up:
            self._link_up = up
            self._pulse()

    def _set_parked_seen(self, seen: bool) -> None:
        if seen != self._parked_seen:
            self._parked_seen = seen
            self._pulse()

    def _guest_ready(self) -> bool:
        # "running" is set only once hello went to this run's guest (or a
        # restored guest that keeps its hello reconnected): see _mark_running
        return (self.vm_state == "running" and self.sandbox.is_alive()
                and self._channel is not None and not self._channel.closed)

    # ------------------------------------------------------------ the task API's VM half
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
        await self._until(lambda: self.vm_state != "parking")

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
        try:
            return await self.park("shutdown",
                                   timeout_s=min(self.settings.park_timeout_s, timeout_s))
        except Exception:
            logger.exception("session %s: park at shutdown failed", self.session_key)
            return False

    # ------------------------------------------------------------ host events, ports
    def _vm_event(self, op: str, source: str | None, t0: float, **extra) -> None:
        """vm{op, source, ms}: ms is the sandbox's own figure for the op when
        it reports one (`last_vm_event`), else the host's wall time; ready_ms
        is the host's (restore/boot: until the guest's link is up)."""
        ready_ms = elapsed_ms(t0)
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
        """Listen again before a restore or boot. The :5000 listener's
        generation is the one this guest run's link must come in on
        (Worker._on_link)."""
        if self.endpoints is None:
            return
        self.endpoints.reopen(self.ports["control"])
        self._control_gen = self.endpoints.generation(self.ports["control"])
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
        self._set_link(False)

    async def _wait_link(self) -> bool:
        await self._until(lambda: self._link_up, timeout=self.settings.hello_timeout_s)
        return self._link_up

    # ------------------------------------------------------------ boot
    async def _start_guest(self, snapshot_dir: Path | None) -> tuple[str, str, float] | None:
        """Start a guest run: restore `snapshot_dir` (None: the BASE snapshot)
        and wait for its link. A base that cannot be restored falls back to a
        rootfs start; a session snapshot that cannot be restored, or a guest
        that never connects, gives None (vm{ok: false} logged, sandbox
        stopped). Returns (op, source, t0) for _mark_running."""
        t0 = time.monotonic()
        self._drop_link()
        if self.endpoints is not None:
            # a fresh :5000 listener for this run (_open_guest_ports below): a
            # connection the previous guest made before it was stopped (its
            # reconnect after the link dropped), queued or accepted, is on the
            # old listener and never taken for this run's link. No generation
            # matches until then, so a stale `link up` the pump handles while
            # the stop below runs is refused too
            self.endpoints.close_port(self.ports["control"])
            self._control_gen = -1
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
            raise GuestError(f"guest did not connect within {self.settings.hello_timeout_s}s")
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

    # ------------------------------------------------------------ park / wake
    async def park(self, reason: str = "requested", *, idle: bool = False,
                   timeout_s: float | None = None) -> bool:
        """Park the guest: send `park`, wait (timeout_s, default
        park_timeout_s) for the acked `parked`, snapshot the VM to
        {session}/vm/snapshot. True when parked. The next
        act()/ui_response/side_result restores it.

        idle=True (the idle timer): only while no task runs, and the VM
        is destroyed afterwards (also a guest that could not park)."""
        self._ensure_plumbing()
        async with self._vm_lock:
            if idle and (self._task_lock.locked() or self._current_future() is not None):
                return False            # a task arrived: not idle any more
            parked = False
            if self._guest_ready():
                self.vm_state = "parking"
                self._set_parked_seen(False)
                self.send_control("park", {"reason": reason})
                parked = await self._park_locked(reason, timeout_s=timeout_s)
            if idle and not self._task_lock.locked() and (parked or self.sandbox.is_alive()):
                await self._destroy("idle")
            return parked

    async def _park_locked(self, reason: str, question: dict | None = None, *,
                           timeout_s: float | None = None) -> bool:
        """Wait for the acked `parked`, snapshot, parked. `question`
        ({input_id, question_id}): the guest parked on that question."""
        timeout_s = self.settings.park_timeout_s if timeout_s is None else timeout_s
        await self._until(lambda: self._parked_seen or self.vm_state != "parking",
                          timeout=timeout_s)
        if self.vm_state != "parking":
            return False                # the guest died meanwhile: the death path owns it
        if not self._parked_seen:
            logger.warning("session %s: no parked within %ss", self.session_key, timeout_s)
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
        idle_s = self.settings.session_idle_s
        if idle_s is None or idle_s <= 0 or self._loop is None:
            return
        self._idle_handle = self._loop.call_later(idle_s, lambda: self._spawn(self._idle_park()))

    async def _idle_park(self) -> None:
        """session_idle_s after a settle: park + snapshot + destroy."""
        self._idle_handle = None
        if self.closed:
            return
        self.sync.host_event("workflow", {"kind": "idle", "idle_s": self.settings.session_idle_s})
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
            write_json(self.parked_file, info)
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
        self._vm_state = "parked"
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
