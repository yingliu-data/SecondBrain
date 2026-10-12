"""guest.coordinator — the single writer (docs/GUEST.md "Coordinator").

    Coordinator(cfg, state, inbound, log_writer, transport, provider, ops, clock,
                *, hooks=None, image_version="dev", workflow_run_id=None,
                workspace=None, bulk=None)
        .emit(type, body) -> event      assigns seq, stamps ts, folds, updates the
                                        TaskView, hands the event to the log
        .restore(events)                before run(): fold a returned log (guest.restore)
        .run() -> settle body           session_start, then the inbound loop until
                                        settle; may be called again for the next task
        .view                           guest.taskview.TaskView: the derived task bookkeeping
    SystemClock                         now_ms(), now_iso(), timer(delay_s, fn) -> cancel()
    FatalProtocolError                  raised after error{fatal} (main exits 2)
    LOST_CONTENT                        the content of a call lost to a VM restart

Threads: run() is the coordinator thread. Provider calls run in a thread that
posts ("llm", turn_id, response | Exception); operations post ("op", ...) and
("op_input", ...) from the operation manager's threads; timers post ("timer",
kind, token); the transport posts ("control", msg), ("ack", seq) and ("link",
status). Nothing but run() touches State or emits.

Modules: this one holds the loop, turns and tool calls; guest.taskview the
reducer that emit() and restore() share; guest.questions the questions and
the question park; guest.mcp_ops the mcp op lifecycle; guest.park the park,
resume and settle; guest.restore resuming from a returned log. The rules
(wake, sync and async mode, limits, compaction, memory, park, mcp, questions)
are written down in docs/GUEST.md.
"""
from __future__ import annotations

import copy
import json
import math
import threading

from protocol import clock as proto_clock
from protocol.allowlist import is_allowed
from protocol.canonical import canonicalisable_error, derive_id, is_int, sha256_hex
from protocol.events import make_event, validate_event
from protocol.fold import State, apply
from protocol.version import HARNESS_VERSION  # noqa: F401  (re-exported)
from guest import hooks as default_hooks
from guest.config import stream_enabled
from guest.context import build_compaction_request, build_request, estimate_tokens, system_hash
from guest.mcp_ops import McpOpsMixin
from guest.park import (  # noqa: F401  (re-exported)
    PARK_ACK_WAIT_S, PARK_QUESTION, PAUSED_ON_PARK, FatalProtocolError, ParkMixin,
)
from guest.questions import MAX_UNANSWERED, QuestionsMixin  # noqa: F401  (re-exported)
from guest.restore import LOST_CONTENT, RestoreMixin  # noqa: F401  (re-exported)
from guest.taskview import (  # noqa: F401  (re-exported)
    DOOM_LOOP_REPEATS, HOST_CLOCK_KINDS, INPUT_KINDS, QUESTION_TOOL, RESUMED, TaskView,
)
from guest.tools import validate_args

MEMORY_KEYS = ("scope", "op", "slug", "sha256")   # side_result.memory, copied into memory events
CORRECTION_BOUNDS = (0.1, 10.0)   # a usage/estimate ratio outside this is a broken count
ESCALATED = "escalated"           # the abort reason that settles escalated


# ---------------------------------------------------------------- clock
class SystemClock:
    """The real clock and threading.Timer. Tests use a fake with the same shape."""

    def now_ms(self) -> int:
        return proto_clock.now_ms()

    def now_iso(self) -> str:
        return proto_clock.now_iso()

    def timer(self, delay_s: float, fn):
        t = threading.Timer(max(0.0, float(delay_s)), fn)
        t.daemon = True
        t.start()
        return t


# ---------------------------------------------------------------- helpers
def _parse_arguments(raw):
    """vLLM's `arguments` is a JSON string; some servers send a dict. Returns
    (args dict, error text or None)."""
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    if not isinstance(raw, str):
        return {}, f"arguments must be a JSON object string, got {type(raw).__name__}"
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return {}, f"arguments are not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"arguments must be a JSON object, got {type(parsed).__name__}"
    bad = canonicalisable_error(parsed, "arguments")
    if bad is not None:         # json.loads accepts NaN/Infinity; the log cannot hold them
        return {}, f"arguments are not valid JSON: {bad}"
    return parsed, None


def _validate_args(name: str, side: str, args: dict) -> list[str]:
    """guest.tools.validate_args for guest built-ins (the only schemas the
    guest holds); host/device/mcp arguments are validated where they run."""
    if side != "guest":
        return []
    try:
        errors = validate_args(name, args)
    except Exception as exc:   # a validator bug must not take the loop down
        return [f"validator error: {exc}"]
    return [str(e) for e in (errors or [])]


# ---------------------------------------------------------------- coordinator
class Coordinator(QuestionsMixin, McpOpsMixin, ParkMixin, RestoreMixin):
    def __init__(self, cfg, state: State, inbound, log_writer, transport, provider, ops, clock,
                 *, hooks=None, image_version: str = "dev", workflow_run_id: str | None = None,
                 harness_version: str = HARNESS_VERSION, workspace=None, bulk=None):
        self.cfg = cfg
        self.state = state
        self.inbound = inbound
        self.log_writer = log_writer
        self.transport = transport
        self.provider = provider
        self.ops = ops
        self.clock = clock
        self.hooks = hooks if hooks is not None else default_hooks.DEFAULT
        self.image_version = image_version
        self.harness_version = harness_version
        self.workflow_run_id = (workflow_run_id
                                or f"{cfg.principal.user}/{cfg.principal.tenant}/{cfg.session_id}")
        self.limits = cfg.limits
        self._async = cfg.async_tools
        self.view = TaskView(cfg, clock.now_ms)       # every emitted or restored event goes through it

        # the loop's own state (what the log does not say)
        self.settled: dict | None = None
        self._final_turn = False          # the no-tools wrap-up after max_tools
        self.current_turn: str | None = None          # the provider call in flight
        self._preambles: dict[str, str] = {}          # preamble_hash -> text (build_request fills it)
        self._grace_fired = False                     # async: the grace window closed with results unseen
        self._correction = 1.0                        # usage.prompt_tokens / raw estimate
        self._turn_raw: int | None = None             # raw estimate of the turn in flight
        self._tokens_before: int | None = None        # of the running compaction turn
        self._mcp_calls: dict[str, dict] = {}         # call_id -> the mcp op (guest.mcp_ops)
        self._timers: dict[str, tuple] = {}           # token -> (handle, kind, due ms)
        self._link_up = True
        self._turn_deferred = False
        self._suppress_wake = False       # while dispatching a round or abandoning work
        self.acked_seq = 0
        self.last_cseq = 0                # highest control cseq this loop has processed

        # park, resume, settle manifest
        self.workspace = workspace
        self.bulk = bulk
        self._paused: dict[str, tuple] = {}           # token -> (kind, remaining s) while parked
        self._park_reason: str | None = None          # the host's park waiting for its moment
        self._question_park_pending = False           # a park on the oldest of _waiting is due
        self._waiting: set[str] = set()               # questions past their deadline, parked on
        self.parked = False                           # parked: idle until the link comes back
        self._session_start: dict | None = None       # the event (head of the checkpoint chain)
        self._last_checkpoint_hash: str | None = None
        self._last_event: dict | None = None          # the newest event on the log
        self._uploaded: dict[str, str] = {}           # workspace path -> sha256 the host holds
        self._digests: dict[str, tuple] = {}          # workspace path -> (stat key, sha256)
        self._restored: dict | None = None            # set by restore(): what run() must finish

    @property
    def task_input_id(self) -> str | None:
        return self.view.task_input_id

    # ------------------------------------------------------------ emit
    def emit(self, type: str, body: dict) -> dict:
        seq = self.state.last_seq + 1
        event = make_event(self.cfg.session_id, seq, self.clock.now_iso(), type, body)
        apply(self.state, event)        # FoldError here is a coordinator bug: let it surface
        self._track(event)
        self.log_writer.put(event)
        return event

    def _track(self, event: dict) -> None:
        """After the fold, for emitted and restored events alike: the
        TaskView and the checkpoint chain."""
        self.view.apply(self.state, event)
        self._last_event = event
        if event["type"] == "session_start":
            self._session_start = event
        elif event["type"] == "checkpoint":
            self._last_checkpoint_hash = event["body"]["state_hash"]

    def live(self, type: str, body: dict) -> None:
        try:
            self.transport.send_live(type, body)
        except Exception:   # noqa: BLE001 - a dead link must not stop the loop
            pass

    def log(self, message: str, **extra) -> None:
        self.live("log", {"level": extra.pop("level", "info"), "message": message, **extra})

    def _id(self, kind: str) -> str:
        """The derived id minted at the next seq."""
        return derive_id(self.cfg.principal.user, self.cfg.principal.tenant,
                         self.cfg.session_id, self.state.last_seq + 1, kind)

    # ------------------------------------------------------------ timers
    def _arm(self, kind: str, token: str, delay_s: float) -> None:
        self._cancel(token)
        handle = self.clock.timer(delay_s, lambda: self.inbound.put(("timer", kind, token)))
        self._timers[token] = (handle, kind, self.clock.now_ms() + int(float(delay_s) * 1000))

    def _arm_op_timeout(self, call_id: str) -> None:
        timeout = self.view.side_calls[call_id]["timeout_s"]
        if timeout:
            self._arm("op_timeout", f"op:{call_id}", timeout)

    def _cancel(self, token: str) -> None:
        self._paused.pop(token, None)
        entry = self._timers.pop(token, None)
        if entry is not None:
            try:
                entry[0].cancel()
            except Exception:
                pass

    def _cancel_all(self) -> None:
        for token in list(self._timers):
            self._cancel(token)
        self._paused.clear()

    # ------------------------------------------------------------ run
    def run(self) -> dict | None:
        """One task: returns its settle body. May be called again for the
        next task."""
        if self.settled is not None:          # the previous task's: start the next one
            self.settled = None
            self._final_turn = False
        if self.state.last_seq == 0:
            self._emit_session_start()
        if self.view.task_input_id is None:
            self._arm("idle_timer", "idle", self.limits.idle_wake_s)
        self.live("ready", {"session_id": self.cfg.session_id, "seq": self.state.last_seq})
        try:
            if self._restored is not None:
                restored, self._restored = self._restored, None
                self._resume_restored(restored)
            while self.settled is None:
                self._maybe_park()
                if self._async and self.inbound.empty():
                    self._async_tick()        # every queued item handled first (the slurp)
                    if self.settled is not None:
                        break
                item = self.inbound.get()
                self._dispatch(item)
        finally:
            self._cancel_all()
        return self.settled

    def _emit_session_start(self) -> None:
        tools = [{"name": t["name"], "side": t["side"], "pin": t.get("pin")} for t in self.cfg.tools]
        self.emit("session_start", {
            "user": self.cfg.principal.user,
            "tenant": self.cfg.principal.tenant,
            "workflow_run_id": self.workflow_run_id,
            "config_hash": self.cfg.config_hash(),
            "system_hash": system_hash(self.cfg),
            "tools": tools,
            "harness_version": self.harness_version,
            "image_version": self.image_version,
            "resumed": bool(self.cfg.resume),
        })
        if is_int(self.cfg.at):
            self.emit("clock", {"kind": "host_hello", "at": self.cfg.at})

    def _dispatch(self, item) -> None:
        kind = item[0]
        if kind == "control":
            self._on_control(item[1])
        elif kind == "llm":
            self._on_llm(item[1], item[2])
        elif kind == "op":
            self._on_op(item[1], item[2])
        elif kind == "op_input":
            self._on_op_input(item[1], item[2])
        elif kind == "timer":
            self._on_timer(item[1], item[2])
        elif kind == "ack":
            self._on_ack(item[1])
        elif kind == "link":
            self._on_link(item[1])
        else:
            self.log(f"unknown inbound item {kind!r}", level="warning")

    # ------------------------------------------------------------ inbound: control
    def _on_control(self, msg: dict) -> None:
        mtype, body = msg.get("type"), msg.get("body") or {}
        cseq = msg.get("cseq")
        if mtype in INPUT_KINDS:
            self._on_input(mtype, body)
        elif mtype == "ui_response":
            self._on_ui_response(body)
        elif mtype == "side_result":
            self._on_side_result(body)
        elif mtype == "heartbeat":
            at = body.get("at")
            if is_int(at):
                self.emit("clock", {"kind": "host_heartbeat", "at": at})
            else:
                self.emit("clock", {"kind": "host_heartbeat", "at": self.clock.now_ms()})
        elif mtype == "abort":
            self._abort(str(body.get("reason", "abort")))
        elif mtype == "park":
            self._request_park(str(body.get("reason") or "park"))
        elif mtype == "hello":
            self.emit("error", {"class": "fatal", "message": "second hello", "retry": False})
            raise FatalProtocolError("second hello")
        else:
            self.log(f"unknown control type {mtype!r}", level="warning")
        # acking cseq is the transport's job (it acks on delivery); this is
        # only the loop's own bookkeeping of what it has processed
        if isinstance(cseq, int):
            self.last_cseq = max(self.last_cseq, cseq)

    def _on_input(self, kind: str, body: dict) -> None:
        input_id = body.get("input_id")
        text = body.get("text", "")
        if not isinstance(input_id, str) or not input_id:
            self.log("input without input_id ignored", level="warning")
            return
        event_body = {"input_id": input_id, "kind": kind, "text": str(text)}
        if "attachments" in body:
            event_body["attachments"] = body["attachments"]
        if is_int(body.get("wall_clock_s"), 1):
            event_body["wall_clock_s"] = body["wall_clock_s"]
        if input_id in self.state.seen_input_ids:
            self.emit("input", {**event_body, "dedupe": "duplicate"})
            return
        starts_task = self.view.task_input_id is None
        self.emit("input", {**event_body, "dedupe": "new"})   # TaskView stages it (or starts the task)
        if starts_task:
            self._start_task()
        # else: staged; the next turn (after the running round, question or
        # in-flight provider call) includes it and names it in its wake

    def _start_task(self) -> None:
        self._cancel("idle")
        wall = self._task_wall_s()
        if wall:
            self._arm("wall_clock", "wall", float(wall))
        self._start_turn()

    def _task_wall_s(self):
        """The running task's wall clock: what its task message named
        (`wall_clock_s`, the host's per-task value), else hello
        limits.wall_clock_s."""
        tid = self.view.task_input_id
        wall = self.view.task_walls.get(tid) if tid else None
        return wall or self.limits.wall_clock_s

    def _on_side_result(self, body: dict) -> None:
        call_id = body.get("call_id")
        pending = self.view.side_calls.get(call_id)
        if pending is None:
            self.log(f"side_result for unknown or terminal call {call_id!r} ignored", level="warning")
            return
        self._cancel(f"op:{call_id}")
        ok = bool(body.get("ok", True))
        content = body.get("content", "")
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        duration = max(0, self.clock.now_ms() - pending["started_ms"])
        self._finish_call(call_id, "done" if ok else "failed", content,
                          memory=body.get("memory"), duration_ms=duration)

    # ------------------------------------------------------------ inbound: others
    def _on_ack(self, seq: int) -> None:
        self.acked_seq = max(self.acked_seq, seq)
        try:
            self.log_writer.mark_acked(seq)
        except Exception as exc:    # noqa: BLE001
            self.log(f"mark_acked failed: {exc}", level="debug")

    def _on_link(self, status: str) -> None:
        self._link_up = status == "up"
        self.log(f"link {status}", level="info")
        if self._link_up and self.parked:
            self._unpark()
        if self._link_up and self._turn_deferred and not self._park_wanted():
            self._turn_deferred = False
            self._start_turn()

    def _on_timer(self, kind: str, token: str) -> None:
        if token not in self._timers:
            return                      # stale (cancelled or already fired)
        self._timers.pop(token, None)
        if kind == "wall_clock":
            self.emit("clock", {"kind": "wall_clock", "at": self.clock.now_ms()})
            self._abandon_open_work("timeout")
            self._settle("timeout", "", reason="wall_clock")
        elif kind == "question_deadline":
            self._expire_question(token.split(":", 1)[1])
        elif kind == "grace_expiry":
            self.emit("clock", {"kind": "grace_expiry", "at": self.clock.now_ms()})
            self.log("grace expired with operations running", level="debug",
                     running=sorted(self.view.turn_pending))
            # async: the results so far wake the model now (wake{grace}); with
            # none yet, the first one to land does (wake{tool_result})
            self._grace_fired = self._async and bool(self.view.unseen)
        elif kind == "idle_timer":
            self.emit("clock", {"kind": "idle_timer", "at": self.clock.now_ms()})
            self.log("idle", level="info")
        elif kind == "grace_park":
            self._on_grace_park()
        elif kind == "op_timeout":
            call_id = token.split(":", 1)[1]
            pending = self.view.side_calls.get(call_id)
            if pending is None:
                return
            duration = max(0, self.clock.now_ms() - pending["started_ms"])
            self._finish_call(call_id, "timeout",
                              f"error: {pending['side']}-side call timed out after {pending['timeout_s']} s",
                              duration_ms=duration)
        elif kind == "mcp_timeout":
            self._mcp_timeout(token.split(":", 1)[1])

    def _on_op(self, call_id: str, result: dict) -> None:
        call = self.state.open_calls.get(call_id)
        if call is None or "result" in call or call_id not in self.view.turn_pending:
            self.log(f"op result for unknown or terminal call {call_id!r} ignored", level="warning")
            return
        state = result.get("state", "done" if result.get("ok") else "failed")
        if state not in ("done", "failed", "timeout", "killed", "lost"):
            state = "done" if result.get("ok") else "failed"
        content = result.get("content", "")
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        spill = result.get("spill")
        spill_ref = None
        if isinstance(spill, dict) and spill.get("path"):
            path = spill.get("bulk_path") or spill["path"]   # bulk_path: the file the host holds
            self.emit("bulk", {"kind": "spill", "path": path, "sha256": spill.get("sha256", ""),
                               "bytes": int(spill.get("bytes", 0))})
            spill_ref = path
        extra = {}
        duration = result.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            extra["duration_ms"] = duration
        self._finish_call(call_id, state, content, spill_ref=spill_ref, **extra)

    # ------------------------------------------------------------ turns
    def _tree_id(self):
        try:
            return self.ops.workspace_tree_id()
        except Exception as exc:    # noqa: BLE001 - a hashing problem is not a loop problem
            self.log(f"workspace_tree_id failed: {exc}", level="warning")
            return None

    def _window_budget(self):
        """context_window_tokens - reserve_tokens, or None when no window is set."""
        window = self.limits.context_window_tokens
        if not window:
            return None
        return max(0, int(window) - int(self.limits.reserve_tokens or 0))

    def _corrected(self, raw: int) -> int:
        """A raw estimate scaled by the usage correction (never below 1)."""
        return max(1, math.ceil(raw * self._correction))

    def _new_memory(self) -> list:
        """The [new memory] slugs: the session memories the fold holds and the
        user-scope slugs this task wrote (the fold only counts those)."""
        return [*self.state.session_memory, *self.view.user_memory]

    def _can_compact(self) -> bool:
        """There is a committed prefix to summarise, the last turn was not a
        compaction (never twice in a row), and the prefix is not already just
        the last summary."""
        committed = self.state.committed
        last = self.state.last_turn()
        if not committed or last is None or last["type"] == "compaction":
            return False
        return not (self.state.compacted_through is not None and len(committed) == 1)

    def _ops_this_task(self) -> int:
        return self.state.limits_consumed["ops"] - self.view.ops_base

    def _start_turn(self) -> None:
        """The next regular turn: the limits, its request, and a compaction
        turn first when the estimate is over the window."""
        if self.settled is not None:
            return
        if not self._link_up or self.parked or self._park_wanted():
            self._turn_deferred = True
            self.log("turn deferred: " + ("link down" if not self._link_up else "park"), level="debug")
            return
        max_turns = self.limits.max_turns
        if max_turns and self.view.turns >= max_turns:
            self.emit("error", {"class": "limit", "message": f"max_turns {max_turns} reached",
                                "retry": False})
            if self.view.turn_pending:        # async: calls still running
                self._abandon_open_work("killed")
            self._settle("failed", "", reason="max_turns")
            return
        max_tools = self.limits.max_tools
        if max_tools and self._ops_this_task() >= max_tools:
            self._final_turn = True
        at = self.clock.now_ms()
        request = build_request(self.state, self.cfg, at, attachments=self.view.attachments,
                                preambles=self._preambles, with_tools=not self._final_turn,
                                new_memory=self._new_memory())
        estimate = self._corrected(estimate_tokens(request))
        budget = self._window_budget()
        if budget is not None and estimate > budget and self._can_compact():
            self.log("compaction: estimate over the window", level="info",
                     estimate=estimate, budget=budget)
            self._start_compaction(estimate)
            return
        self._begin_turn("regular", request, at=at)

    def _start_compaction(self, tokens_before: int) -> None:
        """The compaction turn: its request is the system block, the
        committed prefix and the instruction; includes_through_seq is the
        previous turn's, so it commits nothing and the range it summarises is
        exactly the committed prefix."""
        at = self.clock.now_ms()
        request = build_compaction_request(self.state, self.cfg, preambles=self._preambles)
        self._begin_turn("compaction", request, at=at,
                         includes_through_seq=self.state.last_turn()["includes_through_seq"],
                         tokens_before=tokens_before)

    def _turn_start_body(self, turn_id: str, kind: str, request, includes_through_seq: int) -> dict:
        return {
            "turn_id": turn_id, "type": kind,
            "includes_through_seq": includes_through_seq,
            "model": self.cfg.model, "params": request.params,
            "request_hash": request.request_hash, "preamble_hash": request.preamble_hash,
            "workspace_tree_id": self._tree_id(),
        }

    def _begin_turn(self, kind: str, request, *, at: int, includes_through_seq: int | None = None,
                    tokens_before: int | None = None) -> str:
        """clock{turn_clock, at} -> turn_start -> bookkeeping -> the provider
        call, for both turn types. includes_through_seq None = everything
        logged before the turn_start (a regular turn)."""
        self.emit("clock", {"kind": "turn_clock", "at": at})
        if includes_through_seq is None:
            includes_through_seq = self.state.last_seq
        turn_id = self._id("turn")
        self.emit("turn_start", self._turn_start_body(turn_id, kind, request, includes_through_seq))
        self.current_turn = turn_id
        self._turn_deferred = False
        self._turn_raw = estimate_tokens(request)
        if kind == "regular":
            self._grace_fired = False
        else:
            self._tokens_before = tokens_before
        self._cancel("grace")
        self.log(f"{kind} turn started", level="debug", turn_id=turn_id, final=self._final_turn,
                 estimate=self._corrected(self._turn_raw), tokens_before=tokens_before)
        self._launch(turn_id, request, deltas=kind == "regular")
        return turn_id

    def _launch(self, turn_id: str, request, *, deltas: bool) -> None:
        on_delta = None
        if deltas:
            session_id = self.cfg.session_id

            def on_delta(fragment: dict) -> None:
                # called from the provider thread; transport.send_live only queues.
                # A turn abandoned (settle, abort) while its call still streams
                # must not leak its text into whatever the host shows next.
                if self.current_turn != turn_id or self.settled is not None:
                    return
                self.live("delta", {"session_id": session_id, "turn_id": turn_id,
                                    "kind": fragment["kind"], "index": fragment["index"],
                                    "text": fragment["text"]})
        threading.Thread(target=self._call_provider,
                         args=(turn_id, request, stream_enabled(self.cfg), on_delta),
                         name=f"provider-{turn_id}", daemon=True).start()

    def _call_provider(self, turn_id: str, request, stream: bool, on_delta) -> None:
        try:
            result = self.provider.chat_completion(request, stream=stream, on_delta=on_delta)
        except Exception as exc:    # noqa: BLE001 - everything becomes an inbound item
            result = exc
        self.inbound.put(("llm", turn_id, result))

    def _on_llm(self, turn_id: str, result) -> None:
        if turn_id != self.current_turn or self.settled is not None:
            self.log(f"stale llm result for {turn_id} ignored", level="debug")
            return
        compaction = self.state.turns[turn_id]["type"] == "compaction"
        if isinstance(result, Exception):
            self.current_turn = None
            self._on_provider_error(turn_id, result, compaction)
            return
        body = self._response_body(turn_id, result)
        self.emit("llm_response", body)
        self.current_turn = None
        self._update_correction(body["usage"])
        if compaction:
            self._finish_compaction(turn_id, body["message"])
            return
        self._finish_response(body["message"], body["finish_reason"], turn_id)

    def _finish_response(self, message: dict, finish, turn_id: str | None = None) -> None:
        """What a recorded regular response leads to: its tool calls, a
        staged input's turn, or the settle."""
        if turn_id is None:
            last = self.state.last_turn()
            turn_id = last["turn_id"] if last else ""
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        calls = message.get("tool_calls") or []

        if finish == "length":
            self.emit("error", {"class": "limit", "message": "finish_reason length: no tool runs",
                                "retry": False, "ref": turn_id})
            self._settle("done", text, reason="length")
            return
        if calls and self._final_turn:
            self.log("tool calls in the final no-tools turn ignored", level="warning")
            calls = []
        if calls:
            self._run_tool_calls(turn_id, calls)
            return
        view = self.view
        if self._async and (view.turn_pending or view.unseen):
            return      # no settle while calls run or results are unseen: _async_tick wakes
        if self._final_turn:
            self._settle("done", text, reason="max_tools")
            return
        if view.pending_inputs:
            self._wake("input", [], list(view.pending_inputs))
            self._start_turn()
            return
        self._settle("done", text)

    def _async_tick(self) -> None:
        """Async mode's wake rule (docs/GUEST.md "Async mode"), run by the
        loop whenever the inbound queue is empty, so every item already
        queued (coincident results, an input) is in the same turn. Wakes when
        no provider call is in flight and a result or an input is unseen;
        while the grace window after a response with calls is open, results
        wait for it (or for the last running call), an input does not."""
        view = self.view
        if (self.settled is not None or view.task_input_id is None or self.current_turn is not None
                or view.turns == 0 or not self._link_up or self.parked or self._park_wanted()):
            return
        if not view.unseen and not view.pending_inputs:
            return
        if view.turn_pending and "grace" in self._timers and not view.pending_inputs:
            return
        reason = "grace" if self._grace_fired else "tool_result" if view.unseen else "input"
        self._cancel("grace")
        self._wake(reason, list(view.unseen), list(view.pending_inputs))
        self._start_turn()

    def _wake(self, reason: str, result_ids: list, input_ids: list) -> None:
        self.emit("wake", {"reason": reason, "result_ids": list(result_ids), "input_ids": list(input_ids)})

    @staticmethod
    def _response_body(turn_id: str, result: dict) -> dict:
        """The llm_response body from an assembled chat-completions response."""
        choice = (result.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {"role": "assistant", "content": None})
        message.setdefault("role", "assistant")
        finish = str(choice.get("finish_reason") or "stop")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        body = {"turn_id": turn_id, "message": message, "finish_reason": finish, "usage": usage}
        rid = result.get("id")
        if isinstance(rid, str) or rid is None:
            body["response_id"] = rid
        return body

    def _update_correction(self, usage: dict) -> None:
        """correction = usage.prompt_tokens / the raw estimate of the turn's request."""
        raw, self._turn_raw = self._turn_raw, None
        prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if not raw or not is_int(prompt, 1):
            return
        lo, hi = CORRECTION_BOUNDS
        self._correction = min(hi, max(lo, prompt / raw))

    def _on_provider_error(self, turn_id: str, exc: Exception, during_compaction: bool) -> None:
        # the provider's error protocol: duck-typed flags on the exception
        if getattr(exc, "budget", False):
            self.emit("error", {"class": "budget", "message": f"provider: {exc}",
                                "retry": False, "ref": turn_id})
            self._settle("failed", "", reason="budget")
            return
        if getattr(exc, "overflow", False):
            retry = not during_compaction and self._can_compact()
            self.emit("error", {"class": "overflow", "message": f"provider: {exc}",
                                "retry": retry, "ref": turn_id})
            if retry:
                self._start_compaction(self._corrected(self._turn_raw) if self._turn_raw else 0)
            else:
                self._settle("failed", "", reason="overflow")
            return
        transient = bool(getattr(exc, "transient", False))
        self.emit("error", {"class": "transient" if transient else "protocol",
                            "message": f"provider: {exc}", "retry": False, "ref": turn_id})
        self._settle("failed", "", reason="provider_error")

    def _finish_compaction(self, turn_id: str, summary_message: dict) -> None:
        """compaction + memory after the summary turn's llm_response, then the
        regular turn that was waiting (or being retried). The range is the
        committed prefix the turn summarised: [its first origin, the turn's
        includes_through_seq]."""
        turn = self.state.turns[turn_id]
        content = summary_message.get("content")
        summary = content if isinstance(content, str) else ""
        if not summary:
            self.log("compaction summary is empty", level="warning", turn_id=turn_id)
        body = {"range": [self.state.committed[0]["origin"], turn["includes_through_seq"]],
                "summary_turn_id": turn_id,
                "tokens_before": max(0, int(self._tokens_before or 0)),
                "tokens_after": 0}
        self._tokens_before = None
        body["tokens_after"] = max(0, int(self._estimate_after_compaction(body)))
        self.emit("compaction", body)
        self.emit("memory", {"scope": "session", "op": "write",
                             "slug": f"compaction-{turn['seq']}", "sha256": sha256_hex(summary.strip()),
                             "derived_from": turn_id})
        self.log("compaction done", level="info", range=body["range"],
                 tokens_before=body["tokens_before"], tokens_after=body["tokens_after"])
        self._start_turn()

    def _estimate_after_compaction(self, body: dict) -> int:
        """The estimate of the next regular request, built on a copy of the
        state with this compaction event folded in (the fold's own rule)."""
        preview = copy.deepcopy(self.state)
        try:
            apply(preview, make_event(self.cfg.session_id, preview.last_seq + 1,
                                      self.clock.now_iso(), "compaction", body))
            request = build_request(preview, self.cfg, preview.clock_at or 0,
                                    attachments=self.view.attachments, preambles=dict(self._preambles),
                                    with_tools=not self._final_turn, new_memory=self._new_memory())
        except Exception as exc:    # noqa: BLE001 - reported, never checked
            self.log(f"tokens_after preview failed: {exc}", level="warning")
            return 0
        return self._corrected(estimate_tokens(request))

    # ------------------------------------------------------------ tool calls
    def _side_of(self, name: str) -> str | None:
        for t in self.cfg.tools:
            if t["name"] == name:
                return t["side"]
        return None

    def _run_tool_calls(self, turn_id: str, calls: list) -> None:
        max_tools = self.limits.max_tools
        self._suppress_wake = True
        try:
            for tc in calls:
                if self.settled is not None:    # e.g. a denied question escalated
                    break
                self._run_tool_call(turn_id, tc, max_tools)
        finally:
            self._suppress_wake = False
        if self.settled is not None:
            return
        view = self.view
        grace = self.limits.grace_s
        if self._async:
            # the grace window batches this round's results; _async_tick wakes
            self._grace_fired = False
            if grace and any(c in view.turn_pending for c in view.turn_calls):
                self._arm("grace_expiry", "grace", float(grace))
            return
        if not view.turn_pending:
            self._after_round()
        elif grace:
            self._arm("grace_expiry", "grace", float(grace))

    def _run_tool_call(self, turn_id: str, tc: dict, max_tools) -> None:
        fn = tc.get("function") or {}
        call_id = tc.get("id")
        name = fn.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
            self.log("tool call without id or name ignored", level="warning")
            return
        if call_id in self.state.open_calls:
            self.log(f"duplicate call_id {call_id!r} in response ignored", level="warning")
            return
        side = self._side_of(name)
        args, arg_error = _parse_arguments(fn.get("arguments"))
        if side is None or not is_allowed(self.cfg.tools, name, side):
            # not offered: recorded as a decision only (a tool_call would be a schema error)
            self.emit("guard_decision", {"ref": call_id, "decision": "deny",
                                         "reason": "allowlist", "policy_id": "allowlist"})
            return
        if max_tools and self._ops_this_task() >= max_tools:
            self._deny_call(turn_id, call_id, name, side, args, "limit", "limits",
                            f"error: max_tools ({max_tools}) reached; no more tool calls in this task")
            return
        if arg_error is not None:
            self._deny_call(turn_id, call_id, name, side, args, "malformed_arguments", "validate",
                            f"error: {arg_error}")
            return
        errors = _validate_args(name, side, args)
        if errors:
            self._deny_call(turn_id, call_id, name, side, args, "invalid_arguments", "validate",
                            "error: invalid arguments: " + "; ".join(errors))
            return
        # the doom-loop count is TaskView's (tool_calls after a "hooks" decision)
        if self.view.doom_loop(name, args):
            decision, reason = "ask", "doom_loop"
        else:
            decision, reason = self.hooks.before_tool(
                self.state, {"turn_id": turn_id, "call_id": call_id, "name": name,
                             "side": side, "args": args})
        self.emit("guard_decision", {"ref": call_id, "decision": decision,
                                     "reason": reason, "policy_id": "hooks"})
        self.emit("tool_call", {"turn_id": turn_id, "call_id": call_id, "name": name,
                                "side": side, "args": args, "state": "RUNNING"})
        if decision == "deny":
            self._finish_call(call_id, "failed", f"error: denied by guard ({reason})")
        elif decision == "ask":
            self._ask(call_id,
                      text=(f"The model wants to run {name} with the same arguments for the "
                            f"{DOOM_LOOP_REPEATS}rd time. Continue?" if reason == "doom_loop"
                            else f"Allow {name}? ({reason})"),
                      options=["continue", "stop"])
        else:
            self._run_call(call_id, name, args, side)

    def _deny_call(self, turn_id, call_id, name, side, args, reason, policy, content) -> None:
        self.emit("guard_decision", {"ref": call_id, "decision": "deny",
                                     "reason": reason, "policy_id": policy})
        self.emit("tool_call", {"turn_id": turn_id, "call_id": call_id, "name": name,
                                "side": side, "args": args, "state": "RUNNING"})
        self._finish_call(call_id, "failed", content)

    def _run_call(self, call_id: str, name: str, args: dict, side: str) -> None:
        if side == "guest" and name == QUESTION_TOOL:
            self._ask_tool(call_id, args)
            return
        if side == "mcp":
            self._start_mcp(call_id, name, args)
            return
        if side == "guest":
            self.emit("op_state", {"call_id": call_id, "state": "running"})
            try:
                self.ops.submit(call_id, name, args)
            except Exception as exc:    # noqa: BLE001
                self._finish_call(call_id, "failed", f"error: could not start {name}: {exc}")
            return
        # host/device: TaskView records the side call; side_result answers it
        self.emit("op_state", {"call_id": call_id, "state": "running", "side_ref": side})
        self._arm_op_timeout(call_id)

    def _finish_call(self, call_id: str, state: str, content: str, *, spill_ref=None, memory=None,
                     **op_extra) -> None:
        """op_state{terminal} + tool_result (after_tool rewrite), the host's
        memory fact after an ok result, then the wake check."""
        call = self.state.open_calls.get(call_id)
        if call is None or "result" in call:
            return
        self._mcp_calls.pop(call_id, None)
        self._cancel(f"op:{call_id}")
        self.emit("op_state", {"call_id": call_id, "state": state, **op_extra})
        threshold = self.limits.spill_threshold
        if threshold and len(content) > threshold and spill_ref is None:
            content = content[:threshold] + "\n[truncated]"
        body = {"call_id": call_id, "content": content}
        if spill_ref is not None:
            body["spill_ref"] = spill_ref
        rewrite = self.hooks.after_tool(self.state, {"call_id": call_id, "name": call["name"],
                                                     "side": call["side"]}, content)
        if rewrite is not None:
            body["rewrite"] = rewrite
        if call_id in self.state.placeholders:
            body["late"] = True
        self.emit("tool_result", body)              # TaskView: no longer pending, unseen
        if state == "done" and memory is not None:
            self._emit_memory(call_id, memory)
        if self._async:
            return      # _async_tick wakes once the queue is drained
        view = self.view
        if (not view.turn_pending and view.last_response_had_calls and not self._suppress_wake
                and self.settled is None and self.current_turn is None):
            self._after_round()

    def _emit_memory(self, call_id: str, memory) -> None:
        """memory{scope, op, slug, sha256, derived_from: call_id} from the
        host's side_result.memory, verbatim; a fact the event schema rejects
        is logged and dropped. TaskView keeps the user-scope slugs for the
        preamble."""
        body = {k: memory.get(k) for k in MEMORY_KEYS} if isinstance(memory, dict) else {}
        body["derived_from"] = call_id
        errors = validate_event(make_event(self.cfg.session_id, self.state.last_seq + 1,
                                           self.clock.now_iso(), "memory", body))
        if errors:
            self.log(f"side_result.memory rejected: {errors[0]}", level="warning", call_id=call_id)
            return
        self.emit("memory", body)

    def _after_round(self) -> None:
        """Every call of the round is terminal: wake and start the next turn."""
        self._cancel("grace")
        self._wake("tool_result", list(self.view.turn_calls), list(self.view.pending_inputs))
        self._start_turn()

    # ------------------------------------------------------------ end of task
    def _abandon_open_work(self, state: str) -> None:
        """Kill guest operations, close every open call with an error result."""
        try:
            self.ops.abort_all()
        except Exception as exc:    # noqa: BLE001
            self.log(f"abort_all failed: {exc}", level="warning")
        self._waiting.clear()
        self._suppress_wake = True
        try:
            for qid, pending in list(self.view.questions.items()):
                self._cancel(f"question:{qid}")
                if pending["kind"] == "mcp_input":
                    self._mcp_cancel_input(pending["call_id"])
                self._finish_call(pending["call_id"], state, f"error: {state}")
            for call_id in list(self.view.turn_pending):
                self._finish_call(call_id, state, f"error: {state}")
        finally:
            self._suppress_wake = False
        self.current_turn = None     # a provider result arriving later is stale
        self._tokens_before = None

    def _abort(self, reason: str) -> None:
        """abort{reason}: "escalated" settles escalated (the workflow's
        answer_wait_s ran out), anything else settles aborted; the answer is
        empty and detail carries the reason. settle.reason stays "abort"
        (main exits on it)."""
        self.log(f"abort: {reason}", level="warning")
        self._abandon_open_work("killed")
        outcome = ESCALATED if reason == ESCALATED else "aborted"
        if self.view.task_input_id is None:
            # nothing to settle: no input was ever accepted (settle must name one)
            self._cancel_all()
            self.settled = {"input_id": None, "outcome": outcome, "answer": "",
                            "usage": {}, "manifest": {}, "reason": "abort", "detail": reason}
            return
        self._settle(outcome, "", reason="abort", detail=reason)
