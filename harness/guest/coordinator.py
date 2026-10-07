"""guest.coordinator — the single writer (PLAN.md section 6; docs/GUEST.md).

    Coordinator(cfg, state, inbound, log_writer, transport, provider, ops, clock,
                *, hooks=None, image_version="dev", workflow_run_id=None)
        .emit(type, body) -> event      assigns seq, stamps ts, folds, hands to the log
        .run() -> settle body           session_start, then the inbound loop until settle
    SystemClock                         now_ms(), now_iso(), timer(delay_s, fn) -> cancel()
    FatalProtocolError                  raised after error{fatal} (main exits 2)

Threads: run() is the coordinator thread. Provider calls run in a thread that
posts ("llm", turn_id, response | Exception); operations post ("op", ...) from
the operation manager's threads; timers post ("timer", kind, token). Nothing
but run() touches State or emits.

Time: every timer that has an effect is first recorded as a `clock` event
(grace_expiry, idle_timer, question_deadline, wall_clock); host time arrives
as clock{host_hello} and clock{host_heartbeat}. `ts` on the envelope is
clock.now_iso() and is never read back. Ids are derive_id(user, tenant,
session_id, seq, kind).

Sync mode only (M1): a turn starts when nothing is running and nothing is
awaited. The wake rules:
  - the task's first turn starts on the task input (no wake event: the task is
    the wake, as in the goldens);
  - after a response with tool calls, the next turn starts on the last
    terminal tool_result of the turn: wake{tool_result};
  - after a response without tool calls, staged inputs (a follow_up or steer
    that landed mid-turn) start another turn: wake{input}; otherwise settle;
  - grace: armed after a response with tool calls; if it fires while
    operations still run it is recorded (clock{grace_expiry} + log) and has no
    other effect in sync mode (async mode would start a turn on placeholders).

Limits (cfg.limits): wall_clock_s → settle{timeout}; max_tools → one final
turn without tools, then settle{done}; max_turns → error{limit} +
settle{failed}; finish_reason "length" → no tool runs, settle{done} with the
partial text; question deadline → guard_decision{deny, unanswered}, three →
settle{escalated}; doom loop (same name+args three times) → a question.

Sides: guest tools go to ops.submit; host/device/mcp calls wait for
side_result with cfg.limits.op_timeout_s[side]; the `question` built-in waits
for ui_response. One task per run(): run() returns the settle body.

M1 deviations from the plan:
  - an op timeout has no clock kind in the schema, so it is recorded as
    op_state{timeout, duration_ms} without a clock event;
  - malformed tool arguments get an error result straight away (the repair
    turn is M1b);
  - park is logged and ignored (M3); compaction and workspace_tree_id are M2.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime, timezone

from protocol.allowlist import is_allowed
from protocol.canonical import canonical_json, canonicalisable_error, derive_id, hash_value
from protocol.events import UNANSWERED, make_event
from protocol.fold import State, apply
from guest import hooks as default_hooks
from guest.context import build_request, system_hash

HARNESS_VERSION = "0.0.1"
DOOM_LOOP_REPEATS = 3
MAX_UNANSWERED = 3
QUESTION_TOOL = "question"
INPUT_KINDS = ("task", "follow_up", "steer")


class FatalProtocolError(RuntimeError):
    """A protocol violation the guest cannot continue from (second hello)."""


# ---------------------------------------------------------------- clock
class SystemClock:
    """The real clock and threading.Timer. Tests use a fake with the same shape."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def timer(self, delay_s: float, fn):
        t = threading.Timer(max(0.0, float(delay_s)), fn)
        t.daemon = True
        t.start()
        return t


# ---------------------------------------------------------------- helpers
def _config_dict(cfg) -> dict:
    if dataclasses.is_dataclass(cfg):
        return dataclasses.asdict(cfg)
    try:
        return dict(vars(cfg))
    except TypeError:
        return {"repr": repr(cfg)}


def _config_hash(cfg) -> str:
    """cfg.config_hash() when the config provides it (guest.config does);
    the canonical hash of the whole config otherwise."""
    method = getattr(cfg, "config_hash", None)
    if callable(method):
        return method()
    try:
        return hash_value(_config_dict(cfg))
    except ValueError:
        return hash_value(repr(cfg))


def _limit(limits, name, default):
    return getattr(limits, name, default) if limits is not None else default


def _side_timeout(limits, side: str) -> float | None:
    table = _limit(limits, "op_timeout_s", None) or {}
    v = table.get(side) if isinstance(table, dict) else None
    return float(v) if v else None


def _usage_delta(now: dict, base: dict) -> dict:
    p = now.get("prompt_tokens", 0) - base.get("prompt_tokens", 0)
    c = now.get("completion_tokens", 0) - base.get("completion_tokens", 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


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
        from guest.tools import validate_args
    except Exception:
        return []
    try:
        errors = validate_args(name, args)
    except Exception as exc:   # a validator bug must not take the loop down
        return [f"validator error: {exc}"]
    return [str(e) for e in (errors or [])]


# ---------------------------------------------------------------- coordinator
class Coordinator:
    def __init__(self, cfg, state: State, inbound, log_writer, transport, provider, ops, clock,
                 *, hooks=None, image_version: str = "dev", workflow_run_id: str | None = None,
                 harness_version: str = HARNESS_VERSION):
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
        self.workflow_run_id = (workflow_run_id or getattr(cfg, "workflow_run_id", None)
                                or f"{cfg.principal.user}/{cfg.principal.tenant}/{cfg.session_id}")
        self.limits = getattr(cfg, "limits", None)

        # task bookkeeping (one task per run)
        self.task_input_id: str | None = None
        self.settled: dict | None = None
        self._task_usage_base: dict = {}
        self._task_ops_base = 0
        self._task_turns_base = 0
        self._final_turn = False          # the no-tools wrap-up after max_tools

        # turn bookkeeping
        self.current_turn: str | None = None
        self._turn_calls: list[str] = []  # call_ids of the current round, in order
        self._turn_pending: set[str] = set()
        self._last_response_had_calls = False
        self._preambles: dict[str, str] = {}
        self._attachments: dict[str, list] = {}
        self._call_counts: dict[str, int] = {}
        self._pending_inputs: list[str] = []   # input_ids staged since the last turn start

        # waits
        self._side_calls: dict[str, dict] = {}        # call_id -> {side, started_ms}
        self._questions: dict[str, dict] = {}         # question_id -> {call_id, name, args, kind}
        self._timers: dict[str, object] = {}          # token -> handle
        self._link_up = True
        self._turn_deferred = False
        self._suppress_wake = False       # while dispatching a round or abandoning work
        self.acked_seq = 0
        self.last_cseq = 0                # highest control cseq this loop has processed

    # ------------------------------------------------------------ emit
    def emit(self, type: str, body: dict) -> dict:
        seq = self.state.last_seq + 1
        event = make_event(self.cfg.session_id, seq, self.clock.now_iso(), type, body)
        apply(self.state, event)        # FoldError here is a coordinator bug: let it surface
        self.log_writer.put(event)
        return event

    def live(self, type: str, body: dict) -> None:
        try:
            self.transport.send_live(type, body)
        except Exception as exc:   # a dead link must not stop the loop
            self._debug(f"send_live failed: {exc}", direct=False)

    def log(self, message: str, **extra) -> None:
        self.live("log", {"level": extra.pop("level", "info"), "message": message, **extra})

    def _debug(self, message: str, direct: bool = True) -> None:
        if direct:
            self.log(message, level="debug")

    def _id(self, kind: str) -> str:
        """The derived id minted at the next seq."""
        return derive_id(self.cfg.principal.user, self.cfg.principal.tenant,
                         self.cfg.session_id, self.state.last_seq + 1, kind)

    # ------------------------------------------------------------ timers
    def _arm(self, kind: str, token: str, delay_s: float) -> None:
        self._cancel(token)
        self._timers[token] = self.clock.timer(
            delay_s, lambda: self.inbound.put(("timer", kind, token)))

    def _cancel(self, token: str) -> None:
        handle = self._timers.pop(token, None)
        if handle is not None:
            try:
                handle.cancel()
            except Exception:
                pass

    def _cancel_all(self) -> None:
        for token in list(self._timers):
            self._cancel(token)

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        if self.state.last_seq == 0:
            self._emit_session_start()
        self._arm("idle_timer", "idle", _limit(self.limits, "idle_wake_s", 600))
        self.live("ready", {"session_id": self.cfg.session_id, "seq": self.state.last_seq})
        try:
            while self.settled is None:
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
            "config_hash": _config_hash(self.cfg),
            "system_hash": system_hash(self.cfg),
            "tools": tools,
            "harness_version": self.harness_version,
            "image_version": self.image_version,
            "resumed": bool(getattr(self.cfg, "resume", False)),
        })
        at = getattr(self.cfg, "at", None)
        if isinstance(at, int) and not isinstance(at, bool):
            self.emit("clock", {"kind": "host_hello", "at": at})

    def _dispatch(self, item) -> None:
        kind = item[0]
        if kind == "control":
            self._on_control(item[1])
        elif kind == "llm":
            self._on_llm(item[1], item[2])
        elif kind == "op":
            self._on_op(item[1], item[2])
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
            if isinstance(at, int) and not isinstance(at, bool):
                self.emit("clock", {"kind": "host_heartbeat", "at": at})
            else:
                self.emit("clock", {"kind": "host_heartbeat", "at": self.clock.now_ms()})
        elif mtype == "abort":
            self._abort(str(body.get("reason", "abort")))
        elif mtype == "park":
            self.log("park requested; not supported in M1, ignored", level="warning",
                     reason=body.get("reason"))
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
        if input_id in self.state.seen_input_ids:
            self.emit("input", {**event_body, "dedupe": "duplicate"})
            return
        if isinstance(body.get("attachments"), list):
            self._attachments[input_id] = body["attachments"]
        self.emit("input", {**event_body, "dedupe": "new"})
        self._pending_inputs.append(input_id)
        if self.task_input_id is None:
            self._start_task(input_id)
        # else: staged; the next turn (after the running round, question or
        # in-flight provider call) includes it and names it in its wake

    def _start_task(self, input_id: str) -> None:
        self.task_input_id = input_id
        self._task_usage_base = dict(self.state.usage)
        self._task_ops_base = self.state.limits_consumed["ops"]
        self._task_turns_base = self.state.limits_consumed["turns"]
        self._cancel("idle")
        wall = _limit(self.limits, "wall_clock_s", None)
        if wall:
            self._arm("wall_clock", "wall", float(wall))
        self._start_turn()

    def _on_ui_response(self, body: dict) -> None:
        input_id = body.get("input_id") or f"ui:{body.get('question_id')}"
        qid = body.get("question_id")
        answer = body.get("answer", "")
        answer = "" if answer is None else str(answer)
        base = {"input_id": input_id, "kind": "ui_response", "text": answer,
                "question_id": str(qid), "answer": answer}
        if input_id in self.state.seen_input_ids:
            self.emit("input", {**base, "dedupe": "duplicate"})
            return
        q = self.state.open_questions.get(qid)
        if q is None or q["answered"]:
            self.emit("input", {**base, "dedupe": "late"})
            self.log(f"ui_response for unknown or answered question {qid!r} ignored", level="warning")
            return
        self.emit("input", {**base, "dedupe": "new"})
        self._cancel(f"question:{qid}")
        pending = self._questions.pop(qid, None)
        if pending is None:
            return
        call_id = pending["call_id"]
        if pending["kind"] == "ask":
            # a guard "ask" (doom loop or a hook): continue runs the original call
            if answer.strip().lower() in ("continue", "yes", "y", "ok", "go", "proceed", "true"):
                self._run_call(call_id, pending["name"], pending["args"], pending["side"])
            else:
                self._finish_call(call_id, "failed", f"stopped by user: {answer}")
        else:
            self._finish_call(call_id, "done", answer)

    def _on_side_result(self, body: dict) -> None:
        call_id = body.get("call_id")
        pending = self._side_calls.get(call_id)
        if pending is None:
            self.log(f"side_result for unknown or terminal call {call_id!r} ignored", level="warning")
            return
        self._cancel(f"op:{call_id}")
        self._side_calls.pop(call_id, None)
        ok = bool(body.get("ok", True))
        content = body.get("content", "")
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        duration = max(0, self.clock.now_ms() - pending["started_ms"])
        self._finish_call(call_id, "done" if ok else "failed", content, duration_ms=duration)

    # ------------------------------------------------------------ inbound: others
    def _on_ack(self, seq: int) -> None:
        self.acked_seq = max(self.acked_seq, seq)
        mark = getattr(self.log_writer, "mark_acked", None)
        if not callable(mark):
            mark = getattr(getattr(self.log_writer, "eventlog", None), "mark_acked", None)
        if callable(mark):
            try:
                mark(seq)
            except Exception as exc:
                self._debug(f"mark_acked failed: {exc}")

    def _on_link(self, status: str) -> None:
        self._link_up = status == "up"
        self.log(f"link {status}", level="info")
        if self._link_up and self._turn_deferred:
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
            qid = token.split(":", 1)[1]
            self.emit("clock", {"kind": "question_deadline", "at": self.clock.now_ms()})
            self._deny_question(qid)
        elif kind == "grace_expiry":
            self.emit("clock", {"kind": "grace_expiry", "at": self.clock.now_ms()})
            self.log("grace expired with operations running", level="debug",
                     running=sorted(self._turn_pending))
        elif kind == "idle_timer":
            self.emit("clock", {"kind": "idle_timer", "at": self.clock.now_ms()})
            self.log("idle", level="info")
        elif kind == "op_timeout":
            call_id = token.split(":", 1)[1]
            pending = self._side_calls.pop(call_id, None)
            if pending is None:
                return
            side = pending["side"]
            duration = max(0, self.clock.now_ms() - pending["started_ms"])
            self._finish_call(call_id, "timeout",
                              f"error: {side}-side call timed out after {_side_timeout(self.limits, side)} s",
                              duration_ms=duration)

    def _on_op(self, call_id: str, result: dict) -> None:
        call = self.state.open_calls.get(call_id)
        if call is None or "result" in call or call_id not in self._turn_pending:
            self.log(f"op result for unknown or terminal call {call_id!r} ignored", level="warning")
            return
        state = result.get("state", "done" if result.get("ok") else "failed")
        if state not in ("done", "failed", "timeout", "killed"):
            state = "done" if result.get("ok") else "failed"
        content = result.get("content", "")
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        spill = result.get("spill")
        spill_ref = None
        if isinstance(spill, dict) and spill.get("path"):
            self.emit("bulk", {"kind": "spill", "path": spill["path"], "sha256": spill.get("sha256", ""),
                               "bytes": int(spill.get("bytes", 0))})
            spill_ref = spill["path"]
        extra = {}
        duration = result.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            extra["duration_ms"] = duration
        self._finish_call(call_id, state, content, spill_ref=spill_ref, **extra)

    # ------------------------------------------------------------ turns
    def _turns_this_task(self) -> int:
        return self.state.limits_consumed["turns"] - self._task_turns_base

    def _ops_this_task(self) -> int:
        return self.state.limits_consumed["ops"] - self._task_ops_base

    def _start_turn(self) -> None:
        if self.settled is not None:
            return
        if not self._link_up:
            self._turn_deferred = True
            self.log("turn deferred: link down", level="debug")
            return
        max_turns = _limit(self.limits, "max_turns", None)
        if max_turns and self._turns_this_task() >= max_turns:
            self.emit("error", {"class": "limit", "message": f"max_turns {max_turns} reached",
                                "retry": False})
            self._settle("failed", "", reason="max_turns")
            return
        max_tools = _limit(self.limits, "max_tools", None)
        if max_tools and self._ops_this_task() >= max_tools:
            self._final_turn = True
        at = self.clock.now_ms()
        self.emit("clock", {"kind": "turn_clock", "at": at})
        request = build_request(self.state, self.cfg, at, attachments=self._attachments,
                                preambles=self._preambles, with_tools=not self._final_turn)
        turn_id = self._id("turn")
        self.emit("turn_start", {
            "turn_id": turn_id, "type": "regular",
            "includes_through_seq": self.state.last_seq,
            "model": self.cfg.model, "params": request.params,
            "request_hash": request.request_hash, "preamble_hash": request.preamble_hash,
            "workspace_tree_id": None,
        })
        self.current_turn = turn_id
        self._turn_calls, self._turn_pending = [], set()
        self._pending_inputs = []
        self._cancel("grace")
        self.log("turn started", level="debug", turn_id=turn_id, final=self._final_turn)
        threading.Thread(target=self._call_provider, args=(turn_id, request),
                         name=f"provider-{turn_id}", daemon=True).start()

    def _call_provider(self, turn_id: str, request) -> None:
        try:
            result = self.provider.chat_completion(request)
        except Exception as exc:    # noqa: BLE001 - everything becomes an inbound item
            result = exc
        self.inbound.put(("llm", turn_id, result))

    def _on_llm(self, turn_id: str, result) -> None:
        if turn_id != self.current_turn or self.settled is not None:
            self.log(f"stale llm result for {turn_id} ignored", level="debug")
            return
        if isinstance(result, Exception):
            transient = bool(getattr(result, "transient", False))
            self.emit("error", {"class": "transient" if transient else "protocol",
                                "message": f"provider: {result}", "retry": False, "ref": turn_id})
            self.current_turn = None
            self._settle("failed", "", reason="provider_error")
            return
        choice = (result.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {"role": "assistant", "content": None})
        message.setdefault("role", "assistant")
        finish = str(choice.get("finish_reason") or "stop")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        body = {"turn_id": turn_id, "message": message, "finish_reason": finish, "usage": usage}
        rid = result.get("id")
        if isinstance(rid, str) or rid is None:
            body["response_id"] = rid
        self.emit("llm_response", body)
        self.current_turn = None
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        calls = message.get("tool_calls") or []

        if finish == "length":
            self.emit("error", {"class": "limit", "message": "finish_reason length: no tool runs",
                                "retry": False, "ref": turn_id})
            self.live("delta", {"turn_id": turn_id, "kind": "text", "text": text})
            self._settle("done", text, reason="length")
            return
        if calls and self._final_turn:
            self.log("tool calls in the final no-tools turn ignored", level="warning")
            calls = []
        if calls:
            self._last_response_had_calls = True
            self._run_tool_calls(turn_id, calls)
            return
        self._last_response_had_calls = False
        self.live("delta", {"turn_id": turn_id, "kind": "text", "text": text})
        if self._final_turn:
            self._settle("done", text, reason="max_tools")
            return
        if self._pending_inputs:
            self._wake("input", [], list(self._pending_inputs))
            self._start_turn()
            return
        self._settle("done", text)

    def _wake(self, reason: str, result_ids: list, input_ids: list) -> None:
        self.emit("wake", {"reason": reason, "result_ids": list(result_ids), "input_ids": list(input_ids)})

    # ------------------------------------------------------------ tool calls
    def _side_of(self, name: str) -> str | None:
        for t in self.cfg.tools:
            if t["name"] == name:
                return t["side"]
        return None

    def _run_tool_calls(self, turn_id: str, calls: list) -> None:
        self._turn_calls, self._turn_pending = [], set()
        max_tools = _limit(self.limits, "max_tools", None)
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
        if not self._turn_pending:
            self._after_round()
        else:
            grace = _limit(self.limits, "grace_s", 1.0)
            if grace:
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
        key = name + ":" + canonical_json(args)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        if self._call_counts[key] >= DOOM_LOOP_REPEATS:
            self._call_counts[key] = 0
            decision, reason = "ask", "doom_loop"
        else:
            decision, reason = self.hooks.before_tool(
                self.state, {"turn_id": turn_id, "call_id": call_id, "name": name,
                             "side": side, "args": args})
        self.emit("guard_decision", {"ref": call_id, "decision": decision,
                                     "reason": reason, "policy_id": "hooks"})
        self.emit("tool_call", {"turn_id": turn_id, "call_id": call_id, "name": name,
                                "side": side, "args": args, "state": "RUNNING"})
        self._turn_calls.append(call_id)
        self._turn_pending.add(call_id)
        if decision == "deny":
            self._finish_call(call_id, "failed", f"error: denied by guard ({reason})")
        elif decision == "ask":
            self._ask(call_id, "ask", name, args, side,
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
        self._turn_calls.append(call_id)
        self._turn_pending.add(call_id)
        self._finish_call(call_id, "failed", content)

    def _run_call(self, call_id: str, name: str, args: dict, side: str) -> None:
        if side == "guest" and name == QUESTION_TOOL:
            text = args.get("text") or args.get("question") or args.get("prompt") or ""
            options = args.get("options") or []
            options = [str(o) for o in options] if isinstance(options, list) else []
            self._ask(call_id, "question", name, args, side, text=str(text), options=options)
            return
        if side == "guest":
            self.emit("op_state", {"call_id": call_id, "state": "running"})
            try:
                self.ops.submit(call_id, name, args)
            except Exception as exc:    # noqa: BLE001
                self._finish_call(call_id, "failed", f"error: could not start {name}: {exc}")
            return
        self.emit("op_state", {"call_id": call_id, "state": "running", "side_ref": side})
        self._side_calls[call_id] = {"side": side, "started_ms": self.clock.now_ms()}
        timeout = _side_timeout(self.limits, side)
        if timeout:
            self._arm("op_timeout", f"op:{call_id}", timeout)

    def _ask(self, call_id: str, kind: str, name: str, args: dict, side: str, *,
             text: str, options: list) -> None:
        timeout_s = getattr(self.cfg, "question_timeout_s", 0) or 0
        ui_attached = bool(getattr(self.cfg, "ui_attached", False))
        now = self.clock.now_ms()
        deadline = now + int(timeout_s * 1000)
        qid = self._id("question")
        self.emit("question", {"question_id": qid, "call_id": call_id, "text": text,
                               "options": options, "deadline": deadline})
        self._questions[qid] = {"call_id": call_id, "kind": kind, "name": name,
                                "args": args, "side": side}
        if timeout_s <= 0 and not ui_attached:
            self._deny_question(qid)
            return
        if timeout_s > 0:
            self._arm("question_deadline", f"question:{qid}", float(timeout_s))

    def _deny_question(self, qid: str) -> None:
        pending = self._questions.pop(qid, None)
        if pending is None:
            return
        self._cancel(f"question:{qid}")
        self.emit("guard_decision", {"ref": qid, "decision": "deny", "reason": UNANSWERED,
                                     "policy_id": "question"})
        escalate = self.state.denied_questions >= MAX_UNANSWERED
        was = self._suppress_wake
        self._suppress_wake = was or escalate          # no further turn when escalating
        try:
            self._finish_call(pending["call_id"], "failed", "error: question unanswered before its deadline")
        finally:
            self._suppress_wake = was
        if escalate and self.settled is None:
            self._abandon_open_work("killed")
            self._settle("escalated", "", reason="unanswered_questions")

    def _finish_call(self, call_id: str, state: str, content: str, *, spill_ref=None, **op_extra) -> None:
        """op_state{terminal} + tool_result (after_tool rewrite), then the wake check."""
        call = self.state.open_calls.get(call_id)
        if call is None or "result" in call:
            return
        self._side_calls.pop(call_id, None)
        self._cancel(f"op:{call_id}")
        self.emit("op_state", {"call_id": call_id, "state": state, **op_extra})
        threshold = _limit(self.limits, "spill_threshold", None)
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
        self.emit("tool_result", body)
        self._turn_pending.discard(call_id)
        if (not self._turn_pending and self._last_response_had_calls and not self._suppress_wake
                and self.settled is None and self.current_turn is None):
            self._after_round()

    def _after_round(self) -> None:
        """Every call of the round is terminal: wake and start the next turn."""
        self._cancel("grace")
        self._last_response_had_calls = False
        self._wake("tool_result", list(self._turn_calls), list(self._pending_inputs))
        self._start_turn()

    # ------------------------------------------------------------ end of task
    def _abandon_open_work(self, state: str) -> None:
        """Kill guest operations, close every open call with an error result."""
        try:
            self.ops.abort_all()
        except Exception as exc:    # noqa: BLE001
            self.log(f"abort_all failed: {exc}", level="warning")
        self._suppress_wake = True
        try:
            for qid in list(self._questions):
                pending = self._questions.pop(qid)
                self._cancel(f"question:{qid}")
                self._finish_call(pending["call_id"], state, f"error: {state}")
            for call_id in list(self._turn_pending):
                self._finish_call(call_id, state, f"error: {state}")
        finally:
            self._suppress_wake = False
        self._last_response_had_calls = False
        self.current_turn = None     # a provider result arriving later is stale

    def _abort(self, reason: str) -> None:
        self.log(f"abort: {reason}", level="warning")
        self._abandon_open_work("killed")
        if self.task_input_id is None:
            # nothing to settle: no input was ever accepted (settle must name one)
            self._cancel_all()
            self.settled = {"input_id": None, "outcome": "failed", "answer": "",
                            "usage": {}, "manifest": {}, "reason": "abort", "detail": reason}
            return
        self._settle("failed", "", reason="abort", detail=reason)

    def _settle(self, outcome: str, answer: str, **extra) -> None:
        if self.settled is not None:
            return
        self._cancel_all()
        body = {
            "input_id": self.task_input_id or "",
            "outcome": outcome,
            "answer": answer,
            "usage": _usage_delta(self.state.usage, self._task_usage_base),
            "manifest": {},          # workspace manifest: M3
            **extra,
        }
        self.emit("settle", body)
        self.settled = body
