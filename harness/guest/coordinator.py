"""guest.coordinator — the single writer (docs/GUEST.md).

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

Sync mode only: a turn starts when nothing is running and nothing is
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

Sides: guest tools go to ops.submit (mcp: ops.submit_mcp); host/device calls wait for
side_result with cfg.limits.op_timeout_s[side]; the `question` built-in waits
for ui_response. One task per run(): run() returns the settle body.

Streaming, token estimate, compaction, budget, memory:
  - streaming: every regular turn calls the provider with
    stream=config.stream_enabled(cfg) and an on_delta that sends live
    delta{session_id, turn_id, kind, index, text} per fragment straight from
    the provider thread (Transport.send_live only queues, so it is
    thread-safe); compaction turns pass no on_delta. The provider owns the
    fragments: it de-dups across its own retries and, with streaming off,
    calls on_delta once with kind "text" and the whole content. Every delta
    of a turn is therefore sent before its llm_response, which is emitted
    once from the assembled response.
  - token estimate: the raw context.estimate_tokens(request) (ceil(chars/4))
    of the turn in flight is kept in _turn_raw; the corrected estimate is
    max(1, ceil(raw x correction)), correction = usage.prompt_tokens / the
    raw estimate of the request that produced that usage (1.0 until the
    first usage).
  - turns: _start_turn and _start_compaction build the request and decide;
    _begin_turn emits clock{turn_clock} -> turn_start for both and launches
    the call. _on_llm tells the two apart by the folded turn's type.
  - compaction: before a regular turn whose estimate exceeds
    context_window_tokens - reserve_tokens, and on ProviderError.overflow
    (then the regular turn is retried once), a compaction turn runs first:
    clock{turn_clock}, turn_start{type: compaction, includes_through_seq = the
    previous turn's}, llm_response (the summary), compaction{range: [first
    prefix origin, includes_through_seq], ...}, memory{scope: session, slug:
    compaction-<turn_start seq>, sha256: sha256(summary.strip()),
    derived_from: the turn}. The fold rebuilds the prefix (summary first,
    then whatever was staged after the range) and the pending regular turn
    starts. tokens_after is the estimate of the next regular request built on
    a copy of the state with the same compaction event applied (the fold's
    rule, not a copy of it). A compaction never runs twice in a row
    (`_can_compact`): a second overflow, or an overflow while the prefix is
    already just a summary, is error{overflow, retry: false} +
    settle{failed, reason: overflow}. Compaction turns do not count against
    max_turns.
  - budget: ProviderError.budget (the provider classifies 402) is
    error{budget} + settle{failed, reason: budget}, no retry.
  - memory from tools: the host names the memory a side call changed in
    side_result.memory {scope, op, slug, sha256}; an ok tool_result is
    followed by memory{..., derived_from: call_id} verbatim. User-scope slugs
    are kept per task (`_user_memory`) and listed with the session ones in
    the preamble's [new memory] lines.
  - turn_start.workspace_tree_id = ops.workspace_tree_id() (None when the
    operation manager has no such method, as the test fakes).

Known gaps:
  - an op timeout has no clock kind in the schema, so it is recorded as
    op_state{timeout, duration_ms} without a clock event;
  - malformed tool arguments get an error result straight away (there is no
    repair turn yet).

Park, resume and restore:
  - park{reason}: honoured when no provider call is in flight and no guest
    operation runs (host/device/mcp calls and open questions do not hold it
    back). Until then no new turn starts (it is deferred like a turn on a
    dead link). Guest operations still running get limits.park_grace_s; then
    clock{grace_park} and ops.abort_all(): they finish as op_state{killed}.
    Parking = every timer paused (its remaining time kept), checkpoint
    {through_seq, state_hash, prev_hash} + parked{checkpoint_seq, reason},
    a bounded wait until the host acked parked, transport.park() (close the
    link, back to idle-wait). The next ("link", "up") resumes:
    clock{resumed, at} first (it ends the parked stretch on the log), timers
    re-armed with what they had left, a deferred turn starts.
  - checkpoint chain: prev_hash = hash_value(session_start event) for the
    first, the previous checkpoint's state_hash after (fold.verify_checkpoints).
  - restore(events) before run(): the log the host returned on :5001 after a
    guest death (hello{resume: true}) is folded into the state and the loop's
    bookkeeping is rebuilt from it (task, round, side calls, questions,
    preamble registry, attachments, user memory, doom-loop counts). run()
    then emits clock{host_hello}, op_state{lost} + an error tool_result for
    every open call, and wakes: wake{tool_result} + the next turn when a
    round was open, the settle a dead guest owed when the last response had
    no tool calls, otherwise a new turn (a turn whose response never came is
    first recorded as error{transient}). No second session_start. When the
    returned log ends with `parked` (a fresh process after the host stopped a
    parked guest), nothing is lost: side calls and questions wait again, and
    run() emits clock{resumed} first (before clock{host_hello}).
  - settle: with a workspace, settle.manifest = guest.bulk.settle_manifest
    (changed files and the manifest put on :5001 when there is a bulk client).
  - run() may be called again after a settle: each call handles one task
    (the guest stays up for the next one).

MCP calls:
  - an mcp-side call is a guest operation: op_state{running, side_ref:
    "gateway:<upstream>"} -> ops.submit_mcp (POST /mcp tools/call over
    :5011, header X-SB-Call: call_id) -> the ("op", ...) result -> op_state
    + tool_result, as for any guest op. No side_result. Its op timeout is
    cfg.op_timeout_for(name, "mcp") (the hello entry's timeout_s, else
    limits.op_timeout_s.mcp), armed as an mcp_timeout timer (not paused on a
    park); when it fires ops.cancel() closes the connection and the call ends
    op_state{timeout} + an error result.
  - input required: ("op_input", call_id, request) -> the op is suspended
    (no connection; its op timer is cancelled and the time it had left is
    kept) -> op_state{input_required, attempt, input_request:
    {request_state, schema}} + question{call_id, text: message, options
    (single-enum schema) or [], deadline: min(question_timeout_s,
    expires_in_s)}. ui_response -> op_state{running, attempt + 1} and the
    same tools/call re-sent with _meta["sb/input_response"] {request_state,
    action, content} (guest.mcp.input_response), the op timer re-armed with
    what it had left. The question deadline -> clock{question_deadline} +
    guard_decision{deny, unanswered} (counted towards escalation like any
    question) and the re-send (op_state{running}) with action "cancel"; the
    gateway's answer to it is the call's result. Repeatable: every round is
    one more input_required + question on the same call_id. A suspended call
    holds no park (it is "asked").
  - park: an in-flight mcp call does not hold a park
    either (the forwarder owns it). Parking (and a park grace that kills
    guest ops) first detaches every in-flight mcp op (ops.detach_mcp: the
    connection is closed, nothing is reported); its op timer is not paused
    but keeps its absolute due time. On resume each detached op whose time
    is up is recorded op_state{timeout}; the others are re-attached: the
    same request re-sent with X-SB-Reattach: 1. A 410 answer is
    op_state{lost} + an error result (never re-executed). A fresh process
    (restore) never re-attaches: an mcp call whose folded state is running
    is lost, even after a park; one in input_required waits for its answer
    (attempt and input_request come from the folded op_state).

Questions that park the task:
  - limits.answer_wait_s > 0: a question deadline (or a zero timeout without
    UI) is no deny: _question_expired keeps the question open (_waiting, no
    timer) and requests a park with reason "question"; _park writes
    parked{checkpoint_seq, reason: "question", question_id}. An input-required
    mcp op stays suspended (no cancel re-send). After the resume (link up, or
    restore of a log ending with that parked) a ui_response is handled as
    always; a second question already past its deadline parks the task again
    (_park_next_waiting). answer_wait_s == 0: the deny path above.
  - park: parked.wall_used_ms = the task's wall clock used so far (from the
    paused wall timer). restore: the wall clock used = the last park's
    wall_used_ms + the guest-clock time since its clock{resumed} (or after
    the task's start, without a park).
  - question deadlines: parked.question_left_ms = what each open question's
    deadline had left at the park (from the paused timers; 0 for one already
    past it), as the live timers pause while parked. Restoring a parked log
    re-arms each question with what it had left and sends the due ones (0
    left, or a logged clock{question_deadline, ref}) through
    _question_expired, oldest first (no new park when the log's park is on
    one of them).
  - abort{reason}: "escalated" -> settle{escalated}; any other reason ->
    settle{aborted}; answer "", settle.reason "abort", detail the reason.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import threading
import time
from datetime import datetime, timezone

from protocol.allowlist import is_allowed
from protocol.canonical import canonical_json, canonicalisable_error, derive_id, hash_value, is_int, sha256_hex
from protocol.events import OP_INPUT_REQUIRED, UNANSWERED, make_event, validate_event
from protocol.fold import State, apply, state_hash
from protocol.mcp import CALL_TOOL, split_namespaced
from guest import hooks as default_hooks
from guest.config import stream_enabled
from guest.context import build_compaction_request, build_request, estimate_tokens, system_hash
from guest.mcp import input_response, question_options

HARNESS_VERSION = "0.0.1"
DOOM_LOOP_REPEATS = 3
MAX_UNANSWERED = 3
QUESTION_TOOL = "question"
INPUT_KINDS = ("task", "follow_up", "steer")
MEMORY_KEYS = ("scope", "op", "slug", "sha256")   # side_result.memory, copied into memory events
CORRECTION_BOUNDS = (0.1, 10.0)   # a usage/estimate ratio outside this is a broken count
PARK_ACK_WAIT_S = 60.0            # how long a park waits for the host's ack of `parked`
LOST_CONTENT = "error: operation lost (VM restarted)"
PAUSED_ON_PARK = ("wall_clock", "question_deadline", "op_timeout", "idle_timer")
HOST_CLOCK_KINDS = ("host_hello", "host_heartbeat")   # clock events in host time, not the guest's
RESUMED = "resumed"               # clock kind written first on every wake from a park (ends its stretch)
PARK_QUESTION = "question"        # parked.reason of a park on an unanswered question
ESCALATED = "escalated"           # the abort reason that settles escalated


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
        self.workflow_run_id = (workflow_run_id or getattr(cfg, "workflow_run_id", None)
                                or f"{cfg.principal.user}/{cfg.principal.tenant}/{cfg.session_id}")
        self.limits = getattr(cfg, "limits", None)

        # task bookkeeping (one task per run)
        self.task_input_id: str | None = None
        self.settled: dict | None = None
        self._task_usage_base: dict = {}
        self._task_ops_base = 0
        self._task_turns = 0              # regular turns started in this task
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

        # context
        self._correction = 1.0                 # usage.prompt_tokens / raw estimate
        self._turn_raw: int | None = None      # raw estimate of the turn in flight
        self._tokens_before: int | None = None # of the running compaction turn
        self._user_memory: list[str] = []      # user-scope slugs written this task

        # waits
        self._side_calls: dict[str, dict] = {}        # call_id -> {side, timeout_s, started_ms}
        self._questions: dict[str, dict] = {}         # question_id -> {call_id, kind, name, args, side, timeout_s}
        self._mcp_calls: dict[str, dict] = {}         # call_id -> the mcp op's bookkeeping (_new_mcp)
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
        self._uploaded: dict[str, str] = {}           # workspace path -> sha256 the host holds
        self._digests: dict[str, tuple] = {}          # workspace path -> (stat key, sha256)
        self._restored: dict | None = None            # set by restore(): what run() must finish

    # ------------------------------------------------------------ emit
    def emit(self, type: str, body: dict) -> dict:
        seq = self.state.last_seq + 1
        event = make_event(self.cfg.session_id, seq, self.clock.now_iso(), type, body)
        apply(self.state, event)        # FoldError here is a coordinator bug: let it surface
        self._track(event)
        self.log_writer.put(event)
        return event

    def _track(self, event: dict) -> None:
        """The checkpoint chain's bookkeeping, for emitted and restored events."""
        if event["type"] == "session_start":
            self._session_start = event
        elif event["type"] == "checkpoint":
            self._last_checkpoint_hash = event["body"]["state_hash"]

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
        handle = self.clock.timer(delay_s, lambda: self.inbound.put(("timer", kind, token)))
        self._timers[token] = (handle, kind, self.clock.now_ms() + int(float(delay_s) * 1000))

    def _arm_op_timeout(self, call_id: str) -> None:
        timeout = self._side_calls[call_id]["timeout_s"]
        if timeout:
            self._arm("op_timeout", f"op:{call_id}", timeout)

    def _question_timeout(self, expires=None) -> float:
        """A question's timeout: cfg.question_timeout_s, capped by a gateway
        input request's expires_in_s; 0 = no deadline."""
        timeout_s = getattr(self.cfg, "question_timeout_s", 0) or 0
        if expires:
            timeout_s = min(timeout_s, expires) if timeout_s > 0 else expires
        return timeout_s

    def _arm_question(self, qid: str, delay_s: float | None = None) -> None:
        """Arm a question's deadline: its full timeout, or `delay_s` (what it
        had left at a park, on restore); none when that is 0."""
        if delay_s is None:
            delay_s = self._questions[qid]["timeout_s"]
        if delay_s > 0:
            self._arm("question_deadline", f"question:{qid}", float(delay_s))

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

    def _pause_timers(self) -> None:
        """Park: cancel every timer, keeping what the ones that matter had
        left (the parked time does not count, as in a VM snapshot)."""
        now = self.clock.now_ms()
        kept = {}
        for token, (_, kind, due) in list(self._timers.items()):
            if kind in PAUSED_ON_PARK:
                kept[token] = (kind, max(0, due - now) / 1000.0)
            self._cancel(token)
        self._paused = kept

    def _resume_timers(self) -> None:
        paused, self._paused = self._paused, {}
        for token, (kind, remaining) in paused.items():
            self._arm(kind, token, remaining)

    # ------------------------------------------------------------ run
    def run(self) -> dict | None:
        """One task: returns its settle body. May be called again for the
        next task."""
        if self.settled is not None:          # the previous task's: start the next one
            self.settled = None
            self.task_input_id = None
            self._final_turn = False
        if self.state.last_seq == 0:
            self._emit_session_start()
        if self.task_input_id is None:
            self._arm("idle_timer", "idle", _limit(self.limits, "idle_wake_s", 600))
        self.live("ready", {"session_id": self.cfg.session_id, "seq": self.state.last_seq})
        try:
            if self._restored is not None:
                restored, self._restored = self._restored, None
                self._resume_restored(restored)
            while self.settled is None:
                self._maybe_park()
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
        if is_int(at):
            self.emit("clock", {"kind": "host_hello", "at": at})

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
        self._task_turns = 0
        self._user_memory = []
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
        pending = self._forget_question(qid)
        if not self._waiting:
            self._question_park_pending = False   # answered before its park happened
        if pending is None:
            return
        call_id = pending["call_id"]
        if pending["kind"] == "mcp_input":
            request = self.state.open_calls[call_id]["input_request"]
            self._mcp_resend(call_id, input_response(request["request_state"], answer, request["schema"]))
        elif pending["kind"] == "ask":
            # a guard "ask" (doom loop or a hook): continue runs the original call
            if answer.strip().lower() in ("continue", "yes", "y", "ok", "go", "proceed", "true"):
                self._run_call(call_id, pending["name"], pending["args"], pending["side"])
            else:
                self._finish_call(call_id, "failed", f"stopped by user: {answer}")
        else:
            self._finish_call(call_id, "done", answer)
        self._park_next_waiting()

    def _forget_question(self, qid) -> dict | None:
        """Drop a question from the loop's bookkeeping (answered or denied)."""
        self._waiting.discard(qid)
        return self._questions.pop(qid, None)

    def _question_seq(self, qid: str) -> int:
        return self.state.open_questions[qid]["seq"]

    def _oldest_waiting(self) -> str:
        return min(self._waiting, key=self._question_seq)

    def _request_question_park(self, message: str) -> None:
        """Park on the oldest question past its deadline at the loop's
        next _maybe_park (a pending host park is folded into it)."""
        self._question_park_pending = True
        self.log(message, level="info", question_id=self._oldest_waiting())

    def _park_next_waiting(self) -> None:
        """Another question went past its deadline while the task was
        parked on the one just answered: park on that one next."""
        if self._waiting and self.settled is None and not self.parked and not self._question_park_pending:
            self._request_question_park("another question is unanswered: parking for it")

    def _park_wanted(self) -> bool:
        return self._park_reason is not None or self._question_park_pending

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
        self._finish_call(call_id, "done" if ok else "failed", content,
                          memory=body.get("memory"), duration_ms=duration)

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
                     running=sorted(self._turn_pending))
        elif kind == "idle_timer":
            self.emit("clock", {"kind": "idle_timer", "at": self.clock.now_ms()})
            self.log("idle", level="info")
        elif kind == "grace_park":
            running = self._guest_running()
            if not self._park_wanted() or not running:
                return
            self.emit("clock", {"kind": "grace_park", "at": self.clock.now_ms()})
            self.log("park grace expired: killing guest operations", level="warning",
                     running=sorted(running))
            self._detach_mcp()             # the forwarder keeps those; they are re-attached
            try:
                self.ops.abort_all()       # each reports state "killed" -> op_state{killed}
            except Exception as exc:    # noqa: BLE001
                self.log(f"abort_all failed: {exc}", level="warning")
        elif kind == "op_timeout":
            call_id = token.split(":", 1)[1]
            pending = self._side_calls.pop(call_id, None)
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
        if call is None or "result" in call or call_id not in self._turn_pending:
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

    # ------------------------------------------------------------ mcp
    # _mcp_calls[call_id] = {args, attempt, state, timer_ms, input_response, started_ms}:
    #   attempt   the POST in flight or last sent (rpc id <call_id>#<attempt>)
    #   state     "inflight" (a POST runs in ops), "detached" (park: the
    #             forwarder holds it) or "asked" (suspended on an input request)
    #   timer_ms  the op timeout: the absolute due time (ms) while inflight or
    #             detached, the time left (ms) while asked; None = no timeout
    # The name and the input request come from state.open_calls.
    @staticmethod
    def _gateway_ref(name: str, args: dict) -> str:
        """op_state.side_ref of an mcp call: gateway:<upstream> (the target of
        call_tool, the prefix of a promoted upstream__tool), else gateway."""
        target = args.get("name") if name == CALL_TOOL and isinstance(args.get("name"), str) else name
        upstream, tool = split_namespaced(target)
        return f"gateway:{upstream}" if tool else "gateway"

    def _new_mcp(self, call_id: str, name: str, args: dict, *, attempt: int = 0,
                 state: str = "asked") -> dict:
        timeout = self.cfg.op_timeout_for(name, "mcp")
        mcp = {"args": args, "attempt": attempt, "state": state,
               "timer_ms": int(timeout * 1000) if timeout else None,
               "input_response": None, "started_ms": self.clock.now_ms()}
        self._mcp_calls[call_id] = mcp
        return mcp

    def _start_mcp(self, call_id: str, name: str, args: dict) -> None:
        self.emit("op_state", {"call_id": call_id, "state": "running",
                               "side_ref": self._gateway_ref(name, args)})
        self._new_mcp(call_id, name, args)
        self._mcp_post(call_id)

    def _mcp_resend(self, call_id: str, input_response: dict) -> None:
        """The suspended call re-sent with the user's answer (or a cancel):
        op_state{running, attempt + 1}, then the POST."""
        mcp = self._mcp_calls[call_id]
        mcp["attempt"] += 1
        mcp["input_response"] = input_response
        self.emit("op_state", {"call_id": call_id, "state": "running", "attempt": mcp["attempt"]})
        self._mcp_post(call_id)

    def _mcp_post(self, call_id: str) -> None:
        """One POST of the call: from "asked" (the first, or a re-send: the
        time the op had left is armed again) or from "detached" (a re-attach
        after a park: the same request, the op timer's due time unchanged)."""
        mcp = self._mcp_calls[call_id]
        now = self.clock.now_ms()
        reattach = mcp["state"] == "detached"
        if mcp["state"] == "asked" and mcp["timer_ms"] is not None:
            mcp["timer_ms"] += now                       # time left -> due time
        mcp["state"] = "inflight"
        remaining = None
        if mcp["timer_ms"] is not None:
            remaining = max(0.001, (mcp["timer_ms"] - now) / 1000.0)
            self._arm("mcp_timeout", f"op:{call_id}", remaining)
        try:
            # the socket timeout is only a backstop behind the op timer
            self.ops.submit_mcp(call_id, self.state.open_calls[call_id]["name"], mcp["args"],
                                attempt=mcp["attempt"], input_response=mcp["input_response"],
                                reattach=reattach, timeout_s=(remaining + 5.0) if remaining else None)
        except Exception as exc:    # noqa: BLE001
            self._finish_call(call_id, "failed", f"error: could not call the gateway: {exc}")

    def _mcp_timeout(self, call_id: str) -> None:
        mcp = self._mcp_calls[call_id]
        name = self.state.open_calls[call_id]["name"]
        try:
            self.ops.cancel(call_id)
        except Exception as exc:    # noqa: BLE001
            self.log(f"ops.cancel failed: {exc}", level="warning")
        self._finish_call(call_id, "timeout",
                          f"error: mcp call {name} timed out after {self.cfg.op_timeout_for(name, 'mcp'):g} s",
                          duration_ms=max(0, self.clock.now_ms() - mcp["started_ms"]))

    def _on_op_input(self, call_id: str, request: dict) -> None:
        """The gateway needs input: suspend the op and ask (module doc)."""
        mcp = self._mcp_calls.get(call_id)
        call = self.state.open_calls.get(call_id)
        if mcp is None or call is None or "result" in call or call_id not in self._turn_pending:
            self.log(f"input request for unknown or terminal call {call_id!r} ignored", level="warning")
            return
        self._cancel(f"op:{call_id}")
        if mcp["timer_ms"] is not None:
            mcp["timer_ms"] = max(1000, mcp["timer_ms"] - self.clock.now_ms())   # due time -> time left
        mcp["state"] = "asked"
        schema = request.get("schema") or {}
        self.emit("op_state", {"call_id": call_id, "state": OP_INPUT_REQUIRED, "attempt": mcp["attempt"],
                               "input_request": {"request_state": request["request_state"],
                                                 "schema": schema}})
        self._ask(call_id, "mcp_input", call["name"], mcp["args"], "mcp",
                  text=str(request.get("message") or ""), options=question_options(schema),
                  timeout_s=self._question_timeout(request.get("expires_in_s")))

    def _mcp_cancel(self, call_id: str) -> dict:
        """The input response that cancels a suspended call's request."""
        request = self.state.open_calls[call_id]["input_request"]
        return input_response(request["request_state"], "cancel", {})

    def _mcp_cancel_input(self, call_id: str) -> None:
        """Best effort: tell the gateway a suspended call is abandoned."""
        mcp = self._mcp_calls.get(call_id)
        if mcp is None:
            return
        try:
            self.ops.send_mcp(call_id, self.state.open_calls[call_id]["name"], mcp["args"],
                              attempt=mcp["attempt"] + 1, input_response=self._mcp_cancel(call_id))
        except Exception as exc:    # noqa: BLE001
            self.log(f"mcp cancel re-send failed: {exc}", level="warning")

    def _detach_mcp(self) -> None:
        """Park: close every in-flight mcp connection without cancelling the
        op (the forwarder keeps the call)."""
        try:
            detached = self.ops.detach_mcp()
        except Exception as exc:    # noqa: BLE001
            self.log(f"detach_mcp failed: {exc}", level="warning")
            return
        detached = sorted(cid for cid in detached if cid in self._mcp_calls)
        for cid in detached:
            self._mcp_calls[cid]["state"] = "detached"
        if detached:
            self.log("mcp calls detached for the park", level="info", calls=detached)

    def _reattach_mcp(self) -> None:
        """Resume: an expired detached op is a timeout; the others re-attach."""
        now = self.clock.now_ms()
        for cid in [c for c, m in self._mcp_calls.items() if m["state"] == "detached"]:
            due = self._mcp_calls[cid]["timer_ms"]
            if due is not None and now >= due:
                self._mcp_timeout(cid)
            else:
                self._mcp_post(cid)

    # ------------------------------------------------------------ turns
    def _turns_this_task(self) -> int:
        """Regular turns of this task (compaction turns are not counted)."""
        return self._task_turns

    def _tree_id(self):
        fn = getattr(self.ops, "workspace_tree_id", None)
        if not callable(fn):
            return None
        try:
            tid = fn()
        except Exception as exc:    # noqa: BLE001 - a hashing problem is not a loop problem
            self.log(f"workspace_tree_id failed: {exc}", level="warning")
            return None
        return tid if isinstance(tid, str) else None

    def _window_budget(self):
        """context_window_tokens - reserve_tokens, or None when no window is set."""
        window = _limit(self.limits, "context_window_tokens", None)
        if not window:
            return None
        return max(0, int(window) - int(_limit(self.limits, "reserve_tokens", 0) or 0))

    def _corrected(self, raw: int) -> int:
        """A raw estimate scaled by the usage correction (never below 1)."""
        return max(1, math.ceil(raw * self._correction))

    def _new_memory(self) -> list:
        """The [new memory] slugs: the session memories the fold holds and the
        user-scope slugs this task wrote (the fold only counts those)."""
        return [*self.state.session_memory, *self._user_memory]

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
        return self.state.limits_consumed["ops"] - self._task_ops_base

    def _start_turn(self) -> None:
        """The next regular turn: the limits, its request, and a compaction
        turn first when the estimate is over the window."""
        if self.settled is not None:
            return
        if not self._link_up or self.parked or self._park_wanted():
            self._turn_deferred = True
            self.log("turn deferred: " + ("link down" if not self._link_up else "park"), level="debug")
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
        request = build_request(self.state, self.cfg, at, attachments=self._attachments,
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
            self._task_turns += 1
            self._turn_calls, self._turn_pending = [], set()
            self._pending_inputs = []
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
            self._last_response_had_calls = True
            self._run_tool_calls(turn_id, calls)
            return
        self._last_response_had_calls = False
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
                                    attachments=self._attachments, preambles=dict(self._preambles),
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
        self.emit("op_state", {"call_id": call_id, "state": "running", "side_ref": side})
        self._side_calls[call_id] = {"side": side, "timeout_s": self.cfg.op_timeout_for(name, side),
                                     "started_ms": self.clock.now_ms()}
        self._arm_op_timeout(call_id)

    def _ask(self, call_id: str, kind: str, name: str, args: dict, side: str, *,
             text: str, options: list, timeout_s=None) -> None:
        if timeout_s is None:
            timeout_s = self._question_timeout()
        ui_attached = bool(getattr(self.cfg, "ui_attached", False))
        now = self.clock.now_ms()
        deadline = now + int(timeout_s * 1000)
        qid = self._id("question")
        self.emit("question", {"question_id": qid, "call_id": call_id, "text": text,
                               "options": options, "deadline": deadline})
        self._questions[qid] = {"call_id": call_id, "kind": kind, "name": name,
                                "args": args, "side": side, "timeout_s": timeout_s, "expired": False}
        if timeout_s <= 0 and not ui_attached:
            self._question_expired(qid)
            return
        self._arm_question(qid)

    def _answer_wait(self) -> int:
        return int(_limit(self.limits, "answer_wait_s", 0) or 0)

    def _expire_question(self, qid: str) -> None:
        """A question's deadline timer fired (or, on restore, its log-derived
        due time is behind): clock{question_deadline, ref} names it."""
        self.emit("clock", {"kind": "question_deadline", "at": self.clock.now_ms(), "ref": qid})
        self._question_expired(qid)

    def _question_expired(self, qid: str) -> None:
        """A question's deadline passed unanswered. answer_wait_s > 0:
        the task parks on it (the question stays open; an input-required
        mcp op stays suspended, no cancel); the park itself happens at the
        loop's next _maybe_park, never in the middle of a round. Otherwise the
        question is denied (three denials escalate)."""
        if self._answer_wait() <= 0:
            self._deny_question(qid)
            return
        if qid not in self._questions:
            return
        self._cancel(f"question:{qid}")
        self._waiting.add(qid)
        if self.parked:                      # cannot happen (timers pause): be safe
            return
        if self._question_park_pending:
            self.log("question deadline while parking on another question", level="info",
                     question_id=qid)
            return
        self._request_question_park("question unanswered: parking for the answer")

    def _deny_question(self, qid: str) -> None:
        pending = self._forget_question(qid)
        if pending is None:
            return
        self._cancel(f"question:{qid}")
        self.emit("guard_decision", {"ref": qid, "decision": "deny", "reason": UNANSWERED,
                                     "policy_id": "question"})
        escalate = self.state.denied_questions >= MAX_UNANSWERED
        if pending["kind"] == "mcp_input":
            if escalate:
                self._mcp_cancel_input(pending["call_id"])
                self._abandon_open_work("killed")
                self._settle("escalated", "", reason="unanswered_questions")
                return
            # the gateway answers the cancel with the call's result
            self._mcp_resend(pending["call_id"], self._mcp_cancel(pending["call_id"]))
            return
        was = self._suppress_wake
        self._suppress_wake = was or escalate          # no further turn when escalating
        try:
            self._finish_call(pending["call_id"], "failed", "error: question unanswered before its deadline")
        finally:
            self._suppress_wake = was
        if escalate and self.settled is None:
            self._abandon_open_work("killed")
            self._settle("escalated", "", reason="unanswered_questions")

    def _finish_call(self, call_id: str, state: str, content: str, *, spill_ref=None, memory=None,
                     **op_extra) -> None:
        """op_state{terminal} + tool_result (after_tool rewrite), the host's
        memory fact after an ok result, then the wake check."""
        call = self.state.open_calls.get(call_id)
        if call is None or "result" in call:
            return
        self._side_calls.pop(call_id, None)
        self._mcp_calls.pop(call_id, None)
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
        if state == "done" and memory is not None:
            self._emit_memory(call_id, memory)
        self._turn_pending.discard(call_id)
        if (not self._turn_pending and self._last_response_had_calls and not self._suppress_wake
                and self.settled is None and self.current_turn is None):
            self._after_round()

    def _emit_memory(self, call_id: str, memory) -> None:
        """memory{scope, op, slug, sha256, derived_from: call_id} from the
        host's side_result.memory, verbatim; a fact the event schema rejects
        is logged and dropped. User-scope slugs are kept for the preamble."""
        body = {k: memory.get(k) for k in MEMORY_KEYS} if isinstance(memory, dict) else {}
        body["derived_from"] = call_id
        errors = validate_event(make_event(self.cfg.session_id, self.state.last_seq + 1,
                                           self.clock.now_iso(), "memory", body))
        if errors:
            self.log(f"side_result.memory rejected: {errors[0]}", level="warning", call_id=call_id)
            return
        self.emit("memory", body)
        if body["scope"] == "user":
            slug = body["slug"]
            if body["op"] == "write" and slug not in self._user_memory:
                self._user_memory.append(slug)
            elif body["op"] == "remove" and slug in self._user_memory:
                self._user_memory.remove(slug)

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
        self._waiting.clear()
        self._suppress_wake = True
        try:
            for qid in list(self._questions):
                pending = self._questions.pop(qid)
                self._cancel(f"question:{qid}")
                if pending["kind"] == "mcp_input":
                    self._mcp_cancel_input(pending["call_id"])
                self._finish_call(pending["call_id"], state, f"error: {state}")
            for call_id in list(self._turn_pending):
                self._finish_call(call_id, state, f"error: {state}")
        finally:
            self._suppress_wake = False
        self._last_response_had_calls = False
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
        if self.task_input_id is None:
            # nothing to settle: no input was ever accepted (settle must name one)
            self._cancel_all()
            self.settled = {"input_id": None, "outcome": outcome, "answer": "",
                            "usage": {}, "manifest": {}, "reason": "abort", "detail": reason}
            return
        self._settle(outcome, "", reason="abort", detail=reason)

    def _settle(self, outcome: str, answer: str, **extra) -> None:
        if self.settled is not None:
            return
        self._cancel_all()
        self._waiting.clear()
        self._question_park_pending = False
        body = {
            "input_id": self.task_input_id or "",
            "outcome": outcome,
            "answer": answer,
            "usage": _usage_delta(self.state.usage, self._task_usage_base),
            "manifest": self._settle_manifest(),
            **extra,
        }
        self.emit("settle", body)
        self.settled = body

    def _settle_manifest(self) -> dict:
        """The workspace manifest; changed files and the
        manifest go to the host on :5001 first (when there is a client)."""
        if self.workspace is None:
            return {}
        from guest.bulk import settle_manifest
        try:
            return settle_manifest(self.workspace, self.bulk, self._uploaded, self._digests)
        except Exception as exc:    # noqa: BLE001 - a manifest problem must not block the settle
            self.log(f"workspace manifest failed: {exc}", level="warning")
            return {}

    # ------------------------------------------------------------ park
    def _guest_running(self) -> set:
        """Calls of the round that run as guest operations (not host-side
        calls waiting for side_result, not questions waiting for an answer,
        not mcp calls: the forwarder holds those across a park)."""
        asked = {q["call_id"] for q in self._questions.values()}
        return {c for c in self._turn_pending
                if c not in self._side_calls and c not in asked and c not in self._mcp_calls}

    def _request_park(self, reason: str) -> None:
        if self.parked or self._park_wanted():
            self.log("park requested while already parking; ignored", level="info", reason=reason)
            return
        self._park_reason = reason
        self.log("park requested", level="info", reason=reason)
        self._maybe_park()

    def _maybe_park(self) -> None:
        """Park when nothing guest-side is in flight; otherwise make sure the
        park grace runs while guest operations do."""
        if not self._park_wanted() or self.parked:
            return
        if self._guest_running():
            if "park_grace" not in self._timers:
                self._arm("grace_park", "park_grace", float(_limit(self.limits, "park_grace_s", 30.0)))
            return
        if self.current_turn is not None:       # a provider call in flight: its turn ends first
            return
        self._park()

    def _park(self) -> None:
        question = self._oldest_waiting() if self._question_park_pending and self._waiting else None
        reason = PARK_QUESTION if question is not None else self._park_reason
        self._park_reason, self._question_park_pending = None, False
        self._cancel("park_grace")
        self._detach_mcp()
        self._pause_timers()
        if self._session_start is None:
            raise FatalProtocolError("park without a session_start to chain the checkpoint to")
        prev = self._last_checkpoint_hash or hash_value(self._session_start)
        cp = self.emit("checkpoint", {"through_seq": self.state.last_seq,
                                      "state_hash": state_hash(self.state), "prev_hash": prev})
        body = {"checkpoint_seq": cp["seq"], "reason": reason}
        if question is not None:
            body["question_id"] = question
        # what each open question had left (restore re-arms it); 0 = due
        left = {qid: 0 for qid in self._waiting}
        left.update((token.split(":", 1)[1], int(round(remaining * 1000)))
                    for token, (kind, remaining) in self._paused.items() if kind == "question_deadline")
        if left:
            body["question_left_ms"] = left
        wall, paused = _limit(self.limits, "wall_clock_s", None), self._paused.get("wall")
        if wall and paused is not None:          # the task's wall clock used so far (restore reads it)
            body["wall_used_ms"] = max(0, int(round((float(wall) - paused[1]) * 1000)))
        parked = self.emit("parked", body)
        self.parked = True
        self.log("parked", level="info", reason=reason, seq=parked["seq"], question_id=question)
        self.log_writer.flush(PARK_ACK_WAIT_S)
        if not self.transport.wait_acked(parked["seq"], PARK_ACK_WAIT_S):
            self.log(f"parked (seq {parked['seq']}) not acked within {PARK_ACK_WAIT_S:g}s; "
                     f"going idle anyway (it is resent on reconnect)", level="warning")
        self._on_ack(self.transport.acked_seq)
        self.transport.park()

    def _unpark(self) -> None:
        self.parked = False
        self.emit("clock", {"kind": RESUMED, "at": self.clock.now_ms()})
        self.log("resumed after park", level="info")
        self._resume_timers()
        self._reattach_mcp()

    # ------------------------------------------------------------ resume from the log
    def restore(self, events: list) -> None:
        """Fold the log the host returned (hello{resume: true}, bulk get) into
        the state and rebuild the loop's own bookkeeping from it, as the live
        loop would have left it. Call before run(); run() then settles the
        open work (_resume_restored). Raises FoldError on a log that does not
        fold and ValueError on one that belongs elsewhere."""
        if self.state.last_seq:
            raise ValueError("restore() needs a fresh State")
        if not events:
            return
        if events[0].get("type") != "session_start" or events[0].get("session_id") != self.cfg.session_id:
            raise ValueError("the returned log does not start with this session's session_start")
        calls: dict[str, dict] = {}                 # call_id -> tool_call body
        last_guard_policy = None
        # wall clock used by the task: what the last park recorded, plus the
        # guest-clock time since the clock{resumed} that ended its stretch (or
        # since the task's first guest clock event, without a park)
        wall_used_ms, live_since, in_stretch = 0, None, False
        park_question, question_left = None, {}   # the last park's
        for ev in events:
            etype, body = ev["type"], ev["body"]
            if etype == "turn_start" and body["type"] == "regular":
                self._register_preamble(body)
            apply(self.state, ev)
            self._track(ev)
            if etype == "input" and body["dedupe"] == "new" and body["kind"] in INPUT_KINDS:
                if isinstance(body.get("attachments"), list):
                    self._attachments[body["input_id"]] = body["attachments"]
                self._pending_inputs.append(body["input_id"])
                if self.task_input_id is None:
                    self.task_input_id = body["input_id"]
                    self._task_usage_base = dict(self.state.usage)
                    self._task_ops_base = self.state.limits_consumed["ops"]
                    self._task_turns = 0
                    self._user_memory = []
                    wall_used_ms, live_since = 0, None
            elif etype == "input" and body["dedupe"] == "new" and body["kind"] == "ui_response":
                self._forget_question(body.get("question_id"))
            elif etype == "clock" and body["kind"] not in HOST_CLOCK_KINDS:
                in_stretch = in_stretch and body["kind"] != RESUMED
                if live_since is None and not in_stretch and self.task_input_id is not None:
                    live_since = body["at"]
                if body["kind"] == "question_deadline" and body.get("ref") in self._questions:
                    self._questions[body["ref"]]["expired"] = True
            elif etype == "parked":
                wall_used_ms, live_since, in_stretch = body.get("wall_used_ms", wall_used_ms), None, True
                park_question, question_left = body.get("question_id"), body.get("question_left_ms", {})
            elif etype == "turn_start":
                self.current_turn = body["turn_id"]
                if body["type"] == "regular":
                    self._task_turns += 1
                    self._turn_calls, self._turn_pending = [], set()
                    self._pending_inputs = []
            elif etype == "llm_response":
                self.current_turn = None
                self._last_response_had_calls = False
            elif etype == "guard_decision":
                last_guard_policy = body["policy_id"]
                if body["decision"] == "deny" and body["reason"] == UNANSWERED:
                    self._forget_question(body["ref"])
                continue
            elif etype == "tool_call":
                calls[body["call_id"]] = body
                self._turn_calls.append(body["call_id"])
                self._turn_pending.add(body["call_id"])
                self._last_response_had_calls = True
                if last_guard_policy == "hooks":
                    key = body["name"] + ":" + canonical_json(body["args"])
                    self._call_counts[key] = self._call_counts.get(key, 0) + 1
                    if self._call_counts[key] >= DOOM_LOOP_REPEATS:
                        self._call_counts[key] = 0
            elif etype == "op_state" and body["state"] == "running" and body.get("side_ref") \
                    and calls[body["call_id"]]["side"] in ("host", "device"):
                call = calls[body["call_id"]]
                self._side_calls[body["call_id"]] = {
                    "side": call["side"], "timeout_s": self.cfg.op_timeout_for(call["name"], call["side"]),
                    "started_ms": self.clock.now_ms()}
            elif etype == "question":
                call = calls.get(body["call_id"], {})
                if self.state.open_calls[body["call_id"]]["state"] == OP_INPUT_REQUIRED:
                    kind = "mcp_input"
                else:
                    kind = "question" if call.get("name") == QUESTION_TOOL else "ask"
                self._questions[body["question_id"]] = {
                    "call_id": body["call_id"], "kind": kind, "name": call.get("name"),
                    "args": call.get("args", {}), "side": call.get("side", "guest"),
                    "timeout_s": self._question_timeout(), "expired": False}
            elif etype == "tool_result":
                self._turn_pending.discard(body["call_id"])
                self._side_calls.pop(body["call_id"], None)
                # a question whose call has its result is closed (live: answered,
                # denied or abandoned): never re-armed or expired by a restore
                for qid in [q for q, v in self._questions.items() if v["call_id"] == body["call_id"]]:
                    self._forget_question(qid)
            elif etype == "wake" and body["reason"] == "tool_result":
                self._last_response_had_calls = False
            elif etype == "memory" and body["scope"] == "user":
                slug = body["slug"]
                if body["op"] == "write" and slug not in self._user_memory:
                    self._user_memory.append(slug)
                elif body["op"] == "remove" and slug in self._user_memory:
                    self._user_memory.remove(slug)
            elif etype == "settle":
                self.task_input_id = None
                self._pending_inputs = []
                self._turn_calls, self._turn_pending = [], set()
                self._last_response_had_calls = False
                self._final_turn = False
                self._waiting.clear()
                if isinstance(body.get("manifest"), dict):
                    self._uploaded = {f["path"]: f["sha256"] for f in body["manifest"].get("files", [])
                                      if isinstance(f, dict) and "path" in f and "sha256" in f}
            last_guard_policy = None
        # mcp ops from the folded state: running = a POST was in flight, input_required = asked
        for call_id, call in self.state.open_calls.items():
            if call["side"] == "mcp" and "result" not in call \
                    and call["state"] in ("running", OP_INPUT_REQUIRED):
                self._new_mcp(call_id, call["name"], calls[call_id]["args"], attempt=call.get("attempt", 0),
                              state="inflight" if call["state"] == "running" else "asked")
        max_tools = _limit(self.limits, "max_tools", None)
        self._final_turn = bool(self.task_input_id is not None and max_tools
                                and self._ops_this_task() >= max_tools)
        self._restored = {"after_park": events[-1]["type"] == "parked",   # still in its stretch
                          "wall_used_ms": wall_used_ms, "live_since": live_since,
                          "park_question": park_question, "question_left_ms": question_left}

    def _register_preamble(self, body: dict) -> None:
        """The preamble text a regular turn used, rebuilt from the state just
        before its turn_start (the turn_clock, the first staged input's
        attachments, the [new memory] slugs) and kept when its hash matches;
        the committed user message it marked renders with it."""
        from guest.context import preamble_text
        first = next((it["message"].get("input_id") for it in self.state.staged_suffix
                      if it["message"].get("role") == "user"), None)
        text = preamble_text(self.state.clock_at or 0, self._attachments.get(first), self._new_memory())
        if hash_value(text) == body["preamble_hash"]:
            self._preambles[body["preamble_hash"]] = text
        elif first is not None:
            self.log("preamble of a restored turn could not be rebuilt", level="warning",
                     turn_id=body["turn_id"])

    def _resume_restored(self, restored: dict) -> None:
        """After restore(): record the host's time, settle what the dead guest
        left open and wake the task (module doc)."""
        if restored["after_park"]:            # a wake from a park: its stretch ends here
            self.emit("clock", {"kind": RESUMED, "at": self.clock.now_ms()})
            self._restore_clocks(restored)
            self._resume_after_park(restored)
            return
        self._restore_clocks(restored)
        lost_turn = self.current_turn
        if lost_turn is not None:
            self.current_turn = None
            self.emit("error", {"class": "transient", "message": "turn lost (VM restarted)",
                                "retry": True, "ref": lost_turn})
        open_calls = [cid for cid, call in sorted(self.state.open_calls.items(), key=lambda kv: kv[1]["seq"])
                      if "result" not in call]
        self._questions.clear()
        self._side_calls.clear()
        self._mcp_calls.clear()
        self._suppress_wake = True
        try:
            for call_id in open_calls:
                self._turn_pending.add(call_id)
                self._finish_call(call_id, "lost", LOST_CONTENT)
        finally:
            self._suppress_wake = False
        if self.task_input_id is None:
            return
        if self._last_response_had_calls and not self._turn_pending:
            self._after_round()
            return
        last = self.state.last_turn()
        if lost_turn is None and last is not None and last["type"] == "regular" \
                and last["response_seq"] is not None and not self._last_response_had_calls:
            self._finish_response(self._restored_message(last["response_seq"]),
                                  last.get("finish_reason"))
            return
        self._start_turn()

    def _restore_clocks(self, restored: dict) -> None:
        """The host's time, then the task's wall clock with what it has left."""
        at = getattr(self.cfg, "at", None)
        if is_int(at, 1):
            self.emit("clock", {"kind": "host_hello", "at": at})
        if self.task_input_id is not None:
            self._cancel("idle")
            wall = _limit(self.limits, "wall_clock_s", None)
            if wall:
                used_ms = restored["wall_used_ms"]
                if restored["live_since"] is not None:
                    used_ms += max(0, self.clock.now_ms() - restored["live_since"])
                self._arm("wall_clock", "wall", max(1.0, float(wall) - used_ms / 1000.0))

    def _resume_after_park(self, restored: dict) -> None:
        """A fresh process after a park: nothing was lost; wait for the host again."""
        for call_id in self._side_calls:
            self._arm_op_timeout(call_id)
        due = self._restored_question_deadlines(restored["question_left_ms"])
        # a fresh process never re-attaches: in-flight mcp calls are lost
        lost = [cid for cid, m in self._mcp_calls.items() if m["state"] == "inflight"]
        self._suppress_wake = True
        try:
            for cid in lost:
                self._finish_call(cid, "lost", LOST_CONTENT)
            for qid in due:             # past their due time: the normal expiry path
                if self.settled is not None:
                    break
                if self._questions[qid]["expired"]:
                    self._question_expired(qid)
                else:
                    self._expire_question(qid)
        finally:
            self._suppress_wake = False
        if restored["park_question"] in self._waiting:
            self._question_park_pending = False   # the log's own park is that park
        if self.task_input_id is not None and self.settled is None \
                and self.current_turn is None and not self._turn_pending:
            if self._last_response_had_calls:
                self._after_round()
            else:
                self._start_turn()

    def _restored_question_deadlines(self, left_ms: dict) -> list:
        """After a restore of a parked log: re-arm each open question with the
        time it had left at the park (parked.question_left_ms; a parked stretch
        never counts, as live), and return the due ones, oldest first: its
        expiry already logged (`expired`) or nothing left. A question the park
        did not list had no deadline."""
        due = []
        for qid in sorted(self._questions, key=self._question_seq):
            left = left_ms.get(qid)
            if self._questions[qid]["expired"] or left == 0:
                due.append(qid)
            elif left is not None:
                self._arm_question(qid, left / 1000.0)
        return due

    def _restored_message(self, response_seq: int) -> dict:
        for it in [*self.state.staged_suffix, *self.state.committed]:
            if it["seq"] == response_seq:
                return it["message"]
        return {}
