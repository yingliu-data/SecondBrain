"""guest.coordinator with fakes for transport, log writer, ops, provider and clock.

Every scenario ends with `check_log`: the emitted log validates
(protocol.events.validate_log), replays (protocol.fold.replay) and the replayed
state hashes to the same value as the State the coordinator holds — the core
invariant (state = fold(events)).
"""
from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from guest import hooks as hooks_mod
from guest.config import GuestConfig, Limits, Principal
from guest.coordinator import Coordinator, FatalProtocolError
from guest.context import MISSING_TOOL_MESSAGE
from guest.provider import ProviderError
from protocol.allowlist import effective_tools
from protocol.canonical import derive_id
from protocol.events import UNANSWERED, validate_log
from protocol.fold import State, fold, replay, state_hash
from tests.fakes.fake_llm import response, text, tool_calls

SID = "sess-coord"
USER, TENANT = "u", "t"
T0 = 1_790_000_000_000

WALL_S, IDLE_S, GRACE_S, QUESTION_S = 300.0, 900.0, 0.5, 45.0
OP_TIMEOUTS = {"guest": 120, "host": 120, "device": 60, "mcp": 300}


# ---------------------------------------------------------------- fakes
class FakeClock:
    """Manual time. `timer` records handles; `fire(delay_s)` fires the pending
    ones armed with that delay (all of them when None). The coordinator's
    callbacks only post ("timer", ...) items, so firing is safe from the test
    thread."""

    def __init__(self, start_ms: int = T0):
        self.ms = start_ms
        self.handles: list = []
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            self.ms += 1          # strictly increasing, like a real clock
            return self.ms

    def now_iso(self) -> str:
        return f"2026-10-07T12:00:00.{self.ms % 1000:03d}Z"

    def advance(self, ms: int) -> None:
        with self._lock:
            self.ms += ms

    def timer(self, delay_s: float, fn):
        h = _Handle(float(delay_s), fn)
        with self._lock:
            self.handles.append(h)
        return h

    def pending(self, delay_s=None) -> list:
        with self._lock:
            return [h for h in self.handles if not h.done
                    and (delay_s is None or h.delay_s == float(delay_s))]

    def fire(self, delay_s=None) -> int:
        fired = 0
        for h in self.pending(delay_s):
            h.fire()
            fired += 1
        return fired


class _Handle:
    def __init__(self, delay_s, fn):
        self.delay_s, self.fn, self.done = delay_s, fn, False

    def cancel(self):
        self.done = True

    def fire(self):
        if not self.done:
            self.done = True
            self.fn()


class FakeTransport:
    """Records sends; `on_event` hooks let a test play the host (answer a
    device call, answer a question) in reaction to an event."""

    def __init__(self):
        self.events: list = []
        self.live: list = []
        self.acks: list = []
        self.on_event: list = []

    def send_event(self, event):
        self.events.append(event)
        for fn in list(self.on_event):
            fn(event)

    def send_live(self, type, body):
        self.live.append((type, body))

    def send_ack(self, cseq):
        self.acks.append(cseq)

    def live_of(self, type):
        return [b for t, b in self.live if t == type]


class FakeLogWriter:
    def __init__(self, transport):
        self.transport = transport
        self.events: list = []
        self.acked = 0

    def put(self, event):
        self.events.append(event)
        self.transport.send_event(event)

    def mark_acked(self, seq):
        self.acked = max(self.acked, seq)


class FakeOps:
    """Guest-side operations: posts ("op", ...) at once with a scripted
    result (results[name] = str | callable(args) -> str | dict result), or
    holds the call for `complete()` when `manual`."""

    def __init__(self, inbound, results=None, manual=False):
        self.inbound = inbound
        self.results = results or {}
        self.manual = manual
        self.submitted: list = []
        self.aborted = 0
        self._running: set = set()

    def submit(self, call_id, name, args):
        self.submitted.append((call_id, name, args))
        self._running.add(call_id)
        if not self.manual:
            self.complete(call_id, self._result_for(name, args))

    def _result_for(self, name, args):
        r = self.results.get(name, f"{name} ok")
        if callable(r):
            r = r(args)
        return r

    def complete(self, call_id, result=None, **kw):
        if result is None:
            result = kw
        if isinstance(result, str):
            result = {"ok": True, "content": result, "state": "done", "duration_ms": 5, "spill": None}
        self._running.discard(call_id)
        self.inbound.put(("op", call_id, result))

    def abort_all(self):
        self.aborted += 1
        self._running.clear()

    def running(self):
        return set(self._running)


class FakeProvider:
    """Scripted responses; an entry may be a response dict, a callable(request)
    or an Exception to raise. `hold` blocks each call until `release()`."""

    def __init__(self, script=(), hold_at=()):
        self.script = list(script)
        self.requests: list = []
        self.hold = False
        self.hold_at = set(hold_at)      # 1-based call numbers to hold until release()
        self._gate = threading.Event()
        self._gate.set()
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def push(self, *entries):
        self.script.extend(entries)

    def release(self):
        self._gate.set()

    def chat_completion(self, request):
        self.requests.append(request)
        if self.hold or len(self.requests) in self.hold_at:
            self._gate.clear()
            self.hold = False
        self._gate.wait(5)
        if not self.script:
            raise ProviderError(False, 500, "script exhausted")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            entry = entry(request)
        return entry


# ---------------------------------------------------------------- config
def make_cfg(extra_tools=(), **over) -> GuestConfig:
    limits = Limits(wall_clock_s=int(WALL_S), max_tools=over.pop("max_tools", 10),
                    max_turns=over.pop("max_turns", 12), max_tokens=256,
                    op_timeout_s=dict(OP_TIMEOUTS), spill_threshold=over.pop("spill_threshold", 2000),
                    grace_s=GRACE_S, idle_wake_s=int(IDLE_S))
    kw = dict(
        session_id=SID, principal=Principal(user=USER, tenant=TENANT), model="fake-model",
        params={"temperature": 0}, tools=effective_tools({"tools": list(extra_tools)}),
        system_block={"base_prompt": "You are a test.", "profile": "", "memory_index": ""},
        limits=limits, hooks={}, async_tools=False, question_timeout_s=int(QUESTION_S),
        ui_attached=True, ports={"control": 5000, "bulk": 5001, "llm": 5010}, resume=False, at=T0,
    )
    kw.update(over)
    return GuestConfig(**kw)


DEVICE = [{"name": "calendar", "side": "device", "pin": None}]
HOST = [{"name": "web_search", "side": "host", "pin": None}]


# ---------------------------------------------------------------- scenario
class Scenario:
    def __init__(self, cfg=None, script=(), ops_results=None, ops_manual=False, hooks=None, hold_at=()):
        self.cfg = cfg or make_cfg()
        self.inbound = queue.Queue()
        self.transport = FakeTransport()
        self.log = FakeLogWriter(self.transport)
        self.ops = FakeOps(self.inbound, ops_results, ops_manual)
        self.provider = FakeProvider(script, hold_at)
        self.clock = FakeClock()
        self.state = State()
        self.coord = Coordinator(self.cfg, self.state, self.inbound, self.log, self.transport,
                                 self.provider, self.ops, self.clock, hooks=hooks,
                                 image_version="test-image")
        self.cseq = 0
        self.result = None
        self.error = None
        self.thread = None

    # host side
    def control(self, type, body):
        self.cseq += 1
        self.inbound.put(("control", {"session_id": SID, "cseq": self.cseq, "type": type, "body": body}))

    def task(self, text="hello", input_id="in-1", kind="task", **extra):
        self.control(kind, {"input_id": input_id, "text": text, "attachments": [], **extra})

    def answer_questions_with(self, answer):
        def hook(event):
            if event["type"] == "question":
                self.control("ui_response", {"input_id": "ui-" + event["body"]["question_id"],
                                             "question_id": event["body"]["question_id"],
                                             "answer": answer})
        self.transport.on_event.append(hook)

    def answer_side_calls_with(self, content, ok=True):
        def hook(event):
            if event["type"] == "tool_call" and event["body"]["side"] != "guest":
                body = event["body"]
                c = content(body) if callable(content) else content
                self.control("side_result", {"call_id": body["call_id"], "side": body["side"],
                                             "content": c, "ok": ok})
        self.transport.on_event.append(hook)

    # running
    def start(self):
        def target():
            try:
                self.result = self.coord.run()
            except BaseException as exc:    # noqa: BLE001
                self.error = exc
        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        return self

    def run(self, timeout=5.0):
        if self.thread is None:
            self.start()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), f"coordinator did not settle; events: {self.types()}"
        if self.error is not None:
            raise self.error
        return self.result

    def wait_for(self, pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            if self.error is not None:
                raise self.error
            time.sleep(0.005)
        raise AssertionError(f"condition not met; events: {self.types()}")

    def wait_event(self, type, n=1, timeout=5.0):
        self.wait_for(lambda: len(self.of(type)) >= n, timeout)
        return self.of(type)[n - 1]

    # inspection
    @property
    def events(self):
        return list(self.log.events)

    def of(self, type):
        return [e for e in self.events if e["type"] == type]

    def types(self):
        return [e["type"] for e in self.events]

    def bodies(self, type):
        return [e["body"] for e in self.of(type)]


def check_log(scn: Scenario):
    """The core invariant: the log validates, replays, and folds to the
    coordinator's own State."""
    events = scn.events
    assert validate_log(events) == []
    replayed, errors = replay(events)
    assert errors == []
    assert state_hash(replayed) == state_hash(scn.state)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # the fake transport saw exactly the log
    assert scn.transport.events == events
    return replayed


# ---------------------------------------------------------------- emit
def test_emit_assigns_seq_ts_folds_and_hands_to_the_log_writer():
    scn = Scenario()
    ev = scn.coord.emit("session_start", {
        "user": USER, "tenant": TENANT, "workflow_run_id": "w", "config_hash": "c", "system_hash": "s",
        "tools": scn.cfg.tools, "harness_version": "0", "image_version": "i", "resumed": False})
    assert ev["seq"] == 1 and ev["session_id"] == SID and ev["schema"] == 1
    assert ev["ts"].endswith("Z")
    assert scn.state.last_seq == 1 and scn.state.user == USER
    assert scn.log.events == [ev]
    ev2 = scn.coord.emit("clock", {"kind": "host_heartbeat", "at": 5})
    assert ev2["seq"] == 2 and scn.state.clock_at == 5


def test_emit_rejects_an_event_the_fold_rejects():
    from protocol.fold import FoldError
    scn = Scenario()
    with pytest.raises(FoldError):
        scn.coord.emit("clock", {"kind": "turn_clock", "at": 1})   # before session_start


# ---------------------------------------------------------------- text answer
def test_text_answer_settles_with_the_golden_shape():
    scn = Scenario(script=[text("2 + 2 = 4.", usage={"prompt_tokens": 120, "completion_tokens": 9,
                                                    "total_tokens": 129})])
    scn.task("What is 2 + 2?")
    result = scn.run()
    assert scn.types() == ["session_start", "clock", "input", "clock", "turn_start", "llm_response", "settle"]
    start = scn.bodies("session_start")[0]
    assert start["tools"] == scn.cfg.tools
    assert start["workflow_run_id"] == f"{USER}/{TENANT}/{SID}"
    assert start["resumed"] is False and start["image_version"] == "test-image"
    assert scn.bodies("clock")[0] == {"kind": "host_hello", "at": T0}
    assert scn.bodies("clock")[1]["kind"] == "turn_clock"
    turn = scn.bodies("turn_start")[0]
    assert turn["includes_through_seq"] == scn.of("turn_start")[0]["seq"] - 1
    assert turn["turn_id"] == derive_id(USER, TENANT, SID, scn.of("turn_start")[0]["seq"], "turn")
    assert turn["model"] == "fake-model" and turn["workspace_tree_id"] is None
    assert turn["params"] == {"temperature": 0, "max_tokens": 256}
    assert turn["request_hash"] == scn.provider.requests[0].request_hash
    resp = scn.bodies("llm_response")[0]
    assert resp["message"] == {"role": "assistant", "content": "2 + 2 = 4."}
    assert resp["finish_reason"] == "stop" and resp["response_id"].startswith("chatcmpl-")
    assert result == scn.bodies("settle")[0]
    assert result["outcome"] == "done" and result["answer"] == "2 + 2 = 4." and result["input_id"] == "in-1"
    assert result["usage"] == {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129}
    assert scn.transport.live_of("delta") == [{"turn_id": turn["turn_id"], "kind": "text", "text": "2 + 2 = 4."}]
    assert scn.transport.live[0][0] == "ready"
    assert scn.coord.last_cseq == 1 and scn.transport.acks == []   # acking is the transport's job
    check_log(scn)


def test_request_carries_system_prefix_and_preamble():
    scn = Scenario(script=[text("ok")])
    scn.task("hello", attachments=[{"slug": "tea", "body": "likes tea"}])
    scn.run()
    req = scn.provider.requests[0]
    assert req.messages[0]["role"] == "system" and "You are a test." in req.messages[0]["content"]
    assert req.messages[1]["content"].endswith("\n\nhello")
    assert "[recalled memory: tea]\nlikes tea" in req.messages[1]["content"]
    assert scn.bodies("input")[0]["attachments"] == [{"slug": "tea", "body": "likes tea"}]
    assert scn.bodies("turn_start")[0]["preamble_hash"] == req.preamble_hash
    check_log(scn)


# ---------------------------------------------------------------- guest tool round
def test_one_guest_tool_call_round():
    scn = Scenario(script=[tool_calls([("call_1", "bash", {"command": "echo hi"})]), text("It printed hi.")],
                   ops_results={"bash": "hi\n"})
    scn.task("run echo")
    result = scn.run()
    assert scn.types() == [
        "session_start", "clock", "input", "clock", "turn_start", "llm_response",
        "guard_decision", "tool_call", "op_state", "op_state", "tool_result", "wake",
        "clock", "turn_start", "llm_response", "settle"]
    assert scn.bodies("guard_decision") == [{"ref": "call_1", "decision": "allow",
                                             "reason": hooks_mod.PASS_REASON, "policy_id": "hooks"}]
    call = scn.bodies("tool_call")[0]
    assert call == {"turn_id": scn.bodies("turn_start")[0]["turn_id"], "call_id": "call_1", "name": "bash",
                    "side": "guest", "args": {"command": "echo hi"}, "state": "RUNNING"}
    assert [b["state"] for b in scn.bodies("op_state")] == ["running", "done"]
    assert scn.bodies("op_state")[1]["duration_ms"] == 5
    assert scn.bodies("tool_result") == [{"call_id": "call_1", "content": "hi\n"}]
    assert scn.bodies("wake") == [{"reason": "tool_result", "result_ids": ["call_1"], "input_ids": []}]
    assert scn.ops.submitted == [("call_1", "bash", {"command": "echo hi"})]
    assert result["outcome"] == "done" and result["answer"] == "It printed hi."
    # the second request carries the prefix of the first plus the round
    r1, r2 = scn.provider.requests
    assert r2.messages[: len(r1.messages)] == r1.messages
    assert [m["role"] for m in r2.messages] == ["system", "user", "assistant", "tool"]
    assert r2.messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "hi\n"}
    replayed = check_log(scn)
    assert replayed.open_calls["call_1"]["state"] == "done"


def test_spilled_result_emits_bulk_and_spill_ref():
    scn = Scenario(script=[tool_calls([("call_1", "bash", {"command": "cat big"})]), text("done")],
                   ops_results={"bash": lambda a: {"ok": True, "content": "head...\n[truncated]", "state": "done",
                                                   "duration_ms": 9,
                                                   "spill": {"path": "spill/call_1.txt", "sha256": "a" * 64,
                                                             "bytes": 5000}}})
    scn.task("cat")
    scn.run()
    assert scn.bodies("bulk") == [{"kind": "spill", "path": "spill/call_1.txt", "sha256": "a" * 64, "bytes": 5000}]
    assert scn.bodies("tool_result")[0] == {"call_id": "call_1", "content": "head...\n[truncated]",
                                            "spill_ref": "spill/call_1.txt"}
    check_log(scn)


def test_long_result_without_spill_is_truncated_at_threshold():
    scn = Scenario(cfg=make_cfg(spill_threshold=10),
                   script=[tool_calls([("c", "bash", {"command": "x"})]), text("ok")],
                   ops_results={"bash": "0123456789ABCDEF"})
    scn.task("x")
    scn.run()
    assert scn.bodies("tool_result")[0]["content"] == "0123456789\n[truncated]"
    check_log(scn)


def test_failed_op_result_is_recorded_failed():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "false"})]), text("ok")],
                   ops_results={"bash": lambda a: {"ok": False, "content": "exit 1", "state": "failed",
                                                   "duration_ms": 1, "spill": None}})
    scn.task("x")
    scn.run()
    assert scn.bodies("op_state")[-1]["state"] == "failed"
    assert scn.bodies("tool_result")[0]["content"] == "exit 1"
    check_log(scn)


# ---------------------------------------------------------------- sides
def test_device_call_waits_for_side_result():
    scn = Scenario(cfg=make_cfg(DEVICE),
                   script=[tool_calls([("call_dev", "calendar", {"day": "today"})]), text("One event: Standup.")])
    scn.task("calendar?")
    scn.start()
    scn.wait_event("op_state")
    assert scn.bodies("op_state") == [{"call_id": "call_dev", "state": "running", "side_ref": "device"}]
    assert scn.ops.submitted == []
    assert scn.clock.pending(OP_TIMEOUTS["device"])          # the per-side timer is armed
    # nothing happens until the host answers
    time.sleep(0.05)
    assert scn.types()[-1] == "op_state"
    scn.control("side_result", {"call_id": "call_dev", "side": "device",
                                "content": '[{"title":"Standup"}]', "ok": True})
    result = scn.run()
    assert scn.types()[scn.types().index("op_state"):] == [
        "op_state", "op_state", "tool_result", "wake", "clock", "turn_start", "llm_response", "settle"]
    assert scn.bodies("op_state")[1]["state"] == "done" and scn.bodies("op_state")[1]["duration_ms"] >= 0
    assert scn.bodies("tool_result")[0] == {"call_id": "call_dev", "content": '[{"title":"Standup"}]'}
    assert not scn.clock.pending(OP_TIMEOUTS["device"])      # cancelled, never a clock event
    assert result["outcome"] == "done"
    check_log(scn)


def test_host_side_failure_is_a_failed_result():
    scn = Scenario(cfg=make_cfg(HOST), script=[tool_calls([("c", "web_search", {"q": "x"})]), text("sorry")])
    scn.answer_side_calls_with("search backend down", ok=False)
    scn.task("search")
    scn.run()
    assert scn.bodies("op_state")[-1]["state"] == "failed"
    assert scn.bodies("tool_result")[0]["content"] == "search backend down"
    check_log(scn)


def test_side_result_for_unknown_or_terminal_call_is_logged_and_ignored():
    scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c", "calendar", {})]), text("ok")], hold_at={2})
    scn.answer_side_calls_with("[]")
    scn.task("x")
    scn.start()
    scn.wait_event("turn_start", 2)
    scn.control("side_result", {"call_id": "c", "side": "device", "content": "again", "ok": True})
    scn.control("side_result", {"call_id": "nope", "side": "device", "content": "?", "ok": True})
    scn.wait_for(lambda: scn.coord.last_cseq == scn.cseq)
    scn.provider.release()
    scn.run()
    assert len(scn.of("tool_result")) == 1
    assert not [e for e in scn.events if e["type"] == "input"][1:]
    warnings = [b for b in scn.transport.live_of("log") if "ignored" in b["message"]]
    assert len(warnings) >= 2
    check_log(scn)


def test_device_op_timeout_closes_the_call_without_a_clock_event():
    scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c", "calendar", {})]), text("no answer")])
    scn.task("x")
    scn.start()
    scn.wait_event("op_state")
    assert scn.clock.fire(OP_TIMEOUTS["device"]) == 1
    scn.run()
    assert scn.bodies("op_state")[-1]["state"] == "timeout"
    assert "timed out" in scn.bodies("tool_result")[0]["content"]
    assert [b["kind"] for b in scn.bodies("clock")] == ["host_hello", "turn_clock", "turn_clock"]
    check_log(scn)


# ---------------------------------------------------------------- allowlist and validation
def test_allowlist_denial_records_a_decision_only_and_fills_the_gap():
    scn = Scenario(script=[tool_calls([("bad", "not_a_tool", {"x": 1}), ("ok", "bash", {"command": "ls"})]),
                           text("done")], ops_results={"bash": "a b"})
    scn.task("x")
    scn.run()
    decisions = scn.bodies("guard_decision")
    assert decisions[0] == {"ref": "bad", "decision": "deny", "reason": "allowlist", "policy_id": "allowlist"}
    assert [b["call_id"] for b in scn.bodies("tool_call")] == ["ok"]
    r2 = scn.provider.requests[1]
    tools = [m for m in r2.messages if m["role"] == "tool"]
    assert tools == [{"role": "tool", "tool_call_id": "bad", "content": MISSING_TOOL_MESSAGE},
                     {"role": "tool", "tool_call_id": "ok", "content": "a b"}]
    check_log(scn)


def test_side_mismatch_is_an_allowlist_denial():
    """A device tool name is offered only on the device side; the model cannot
    make it a guest call. Here the name is not offered at all."""
    scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c", "reminders", {})]), text("no")])
    scn.task("x")
    scn.run()
    assert scn.bodies("guard_decision")[0]["reason"] == "allowlist"
    assert scn.of("tool_call") == []
    check_log(scn)


def test_malformed_arguments_get_an_error_result_without_running():
    scn = Scenario(script=[tool_calls([("c", "bash", '{"command": ')]), text("ok")])
    scn.task("x")
    scn.run()
    assert scn.bodies("guard_decision")[0] == {"ref": "c", "decision": "deny", "reason": "malformed_arguments",
                                               "policy_id": "validate"}
    assert scn.bodies("tool_call")[0]["args"] == {}
    assert scn.bodies("op_state")[0]["state"] == "failed"
    assert scn.bodies("tool_result")[0]["content"].startswith("error: arguments are not valid JSON")
    assert scn.ops.submitted == []
    check_log(scn)


def test_hook_deny_and_after_tool_rewrite():
    hooks = hooks_mod.Hooks()
    hooks.register_before(lambda state, call: ("deny", "policy") if call["name"] == "write" else ("allow", "ok"))
    hooks.register_after(lambda state, call, content: content.upper())
    scn = Scenario(script=[tool_calls([("w", "write", {"path": "a", "content": "z"}),
                                       ("b", "bash", {"command": "ls"})]), text("ok")],
                   ops_results={"bash": "files"}, hooks=hooks)
    scn.task("x")
    scn.run()
    assert scn.bodies("guard_decision") == [
        {"ref": "w", "decision": "deny", "reason": "policy", "policy_id": "hooks"},
        {"ref": "b", "decision": "allow", "reason": "ok", "policy_id": "hooks"}]
    results = {b["call_id"]: b for b in scn.bodies("tool_result")}
    assert results["w"]["content"].startswith("error: denied by guard")
    assert results["b"] == {"call_id": "b", "content": "files", "rewrite": "FILES"}
    assert scn.provider.requests[1].messages[-1]["content"] == "FILES"
    check_log(scn)


# ---------------------------------------------------------------- questions
def q_call(cid="q1", text="Which one?"):
    return tool_calls([(cid, "question", {"text": text, "options": ["a", "b"]})])


def test_question_answered_by_ui_response():
    scn = Scenario(script=[q_call(), text("Thanks, a it is.")])
    scn.answer_questions_with("a")
    scn.task("ask me")
    scn.run()
    q = scn.bodies("question")[0]
    qseq = scn.of("question")[0]["seq"]
    assert q["question_id"] == derive_id(USER, TENANT, SID, qseq, "question")
    assert q["call_id"] == "q1" and q["text"] == "Which one?" and q["options"] == ["a", "b"]
    assert q["deadline"] > T0
    ui = [b for b in scn.bodies("input") if b["kind"] == "ui_response"]
    assert ui == [{"input_id": "ui-" + q["question_id"], "kind": "ui_response", "text": "a",
                   "question_id": q["question_id"], "answer": "a", "dedupe": "new"}]
    assert scn.bodies("tool_result")[0] == {"call_id": "q1", "content": "a"}
    assert scn.bodies("op_state")[-1]["state"] == "done"
    assert not scn.clock.pending(QUESTION_S)
    replayed = check_log(scn)
    assert replayed.open_questions[q["question_id"]]["answered"] is True


def test_question_deadline_denies_unanswered():
    scn = Scenario(script=[q_call(), text("I will guess.")])
    scn.task("ask")
    scn.start()
    q = scn.wait_event("question")["body"]
    assert scn.clock.fire(QUESTION_S) == 1
    scn.run()
    i = scn.types().index("question")
    assert scn.types()[i:i + 5] == ["question", "clock", "guard_decision", "op_state", "tool_result"]
    assert scn.bodies("clock")[-2]["kind"] == "question_deadline"
    assert scn.bodies("guard_decision")[-1] == {"ref": q["question_id"], "decision": "deny",
                                                "reason": UNANSWERED, "policy_id": "question"}
    assert scn.bodies("tool_result")[0]["content"].startswith("error: question unanswered")
    assert scn.state.denied_questions == 1
    check_log(scn)


def test_three_unanswered_questions_escalate():
    scn = Scenario(script=[q_call("q1"), q_call("q2"), q_call("q3"), text("never reached")])
    scn.task("ask")
    scn.start()
    for n in (1, 2, 3):
        scn.wait_event("question", n)
        assert scn.clock.fire(QUESTION_S) == 1
    result = scn.run()
    assert result["outcome"] == "escalated"
    assert scn.state.denied_questions == 3
    assert len(scn.of("turn_start")) == 3 and len(scn.provider.requests) == 3
    check_log(scn)


def test_question_with_zero_timeout_and_no_ui_is_denied_at_once():
    scn = Scenario(cfg=make_cfg(question_timeout_s=0, ui_attached=False), script=[q_call(), text("ok")])
    scn.task("ask")
    scn.run()
    i = scn.types().index("question")
    assert scn.types()[i:i + 4] == ["question", "guard_decision", "op_state", "tool_result"]
    assert scn.bodies("question")[0]["deadline"] == scn.bodies("question")[0]["deadline"]  # int
    assert scn.bodies("guard_decision")[-1]["reason"] == UNANSWERED
    assert scn.clock.pending(QUESTION_S) == []
    check_log(scn)


def test_ui_response_for_unknown_question_is_recorded_late():
    scn = Scenario(script=[q_call(), text("ok")], hold_at={2})
    scn.answer_questions_with("a")
    scn.task("ask")
    scn.start()
    scn.wait_event("turn_start", 2)
    scn.control("ui_response", {"input_id": "ui-x", "question_id": "nope", "answer": "z"})
    scn.wait_for(lambda: scn.coord.last_cseq == scn.cseq)
    scn.provider.release()
    scn.run()
    late = [b for b in scn.bodies("input") if b["dedupe"] == "late"]
    assert late == [{"input_id": "ui-x", "kind": "ui_response", "text": "z", "question_id": "nope",
                     "answer": "z", "dedupe": "late"}]
    check_log(scn)


def test_doom_loop_asks_and_stop_fails_the_call():
    same = ("bash", {"command": "ls"})
    scn = Scenario(script=[tool_calls([("c1", *same)]), tool_calls([("c2", *same)]),
                           tool_calls([("c3", *same)]), text("giving up")], ops_results={"bash": "x"})
    scn.answer_questions_with("stop")
    scn.task("loop")
    scn.run()
    decisions = {b["ref"]: b for b in scn.bodies("guard_decision")}
    assert decisions["c3"] == {"ref": "c3", "decision": "ask", "reason": "doom_loop", "policy_id": "hooks"}
    assert scn.bodies("question")[0]["call_id"] == "c3"
    assert [c for c, _, _ in scn.ops.submitted] == ["c1", "c2"]
    results = {b["call_id"]: b["content"] for b in scn.bodies("tool_result")}
    assert results["c3"].startswith("stopped by user")
    check_log(scn)


def test_doom_loop_continue_runs_the_call():
    same = ("bash", {"command": "ls"})
    scn = Scenario(script=[tool_calls([("c1", *same)]), tool_calls([("c2", *same)]),
                           tool_calls([("c3", *same)]), text("ok")], ops_results={"bash": "x"})
    scn.answer_questions_with("continue")
    scn.task("loop")
    scn.run()
    assert [c for c, _, _ in scn.ops.submitted] == ["c1", "c2", "c3"]
    assert {b["call_id"]: b["content"] for b in scn.bodies("tool_result")}["c3"] == "x"
    check_log(scn)


# ---------------------------------------------------------------- limits
def test_max_tools_wrap_up_turn_has_no_tools_then_settles_done():
    scn = Scenario(cfg=make_cfg(max_tools=1),
                   script=[tool_calls([("c1", "bash", {"command": "a"})]),
                           tool_calls([("c2", "bash", {"command": "b"})], content="still calling"),
                           text("never")],
                   ops_results={"bash": "out"})
    scn.task("x")
    result = scn.run()
    r1, r2 = scn.provider.requests
    assert r1.tools and r2.tools == []
    assert len(scn.of("tool_call")) == 1
    assert result["outcome"] == "done" and result["answer"] == "still calling" and result["reason"] == "max_tools"
    assert len(scn.provider.script) == 1
    check_log(scn)


def test_calls_beyond_max_tools_in_one_response_are_denied_with_limit():
    scn = Scenario(cfg=make_cfg(max_tools=1),
                   script=[tool_calls([("c1", "bash", {"command": "a"}), ("c2", "bash", {"command": "b"})]),
                           text("done")],
                   ops_results={"bash": "out"})
    scn.task("x")
    scn.run()
    decisions = {b["ref"]: b for b in scn.bodies("guard_decision")}
    assert decisions["c2"]["decision"] == "deny" and decisions["c2"]["reason"] == "limit"
    assert [c for c, _, _ in scn.ops.submitted] == ["c1"]
    check_log(scn)


def test_max_turns_settles_failed_with_an_error_event():
    scn = Scenario(cfg=make_cfg(max_turns=1),
                   script=[tool_calls([("c1", "bash", {"command": "a"})]), text("never")],
                   ops_results={"bash": "out"})
    scn.task("x")
    result = scn.run()
    assert result["outcome"] == "failed" and result["reason"] == "max_turns"
    assert scn.bodies("error")[-1]["class"] == "limit"
    assert scn.types()[-3:] == ["wake", "error", "settle"]
    check_log(scn)


def test_finish_reason_length_runs_no_tools():
    scn = Scenario(script=[response(content="partial", tool_calls=[("c", "bash", {"command": "x"})],
                                    finish_reason="length")])
    scn.task("x")
    result = scn.run()
    assert scn.of("tool_call") == [] and scn.ops.submitted == []
    assert scn.bodies("error")[0]["class"] == "limit"
    assert result["outcome"] == "done" and result["answer"] == "partial" and result["reason"] == "length"
    check_log(scn)


def test_wall_clock_settles_timeout_and_ignores_the_late_response():
    scn = Scenario(script=[text("too late")])
    scn.provider.hold = True
    scn.task("x")
    scn.start()
    scn.wait_event("turn_start")
    assert scn.clock.fire(WALL_S) == 1
    result = scn.run()
    assert result["outcome"] == "timeout"
    assert scn.types()[-2:] == ["clock", "settle"] and scn.bodies("clock")[-1]["kind"] == "wall_clock"
    n = len(scn.events)
    scn.provider.release()
    time.sleep(0.05)
    assert len(scn.events) == n                      # the stale llm item changes nothing
    check_log(scn)


def test_wall_clock_kills_open_side_calls():
    scn = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c", "calendar", {})])])
    scn.task("x")
    scn.start()
    scn.wait_event("op_state")
    scn.clock.fire(WALL_S)
    result = scn.run()
    assert result["outcome"] == "timeout"
    assert scn.bodies("op_state")[-1]["state"] == "timeout"
    assert scn.bodies("tool_result")[0]["call_id"] == "c"
    assert scn.ops.aborted == 1
    check_log(scn)


# ---------------------------------------------------------------- control
def test_abort_mid_call_fails_the_task():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "sleep 100"})])], ops_manual=True)
    scn.task("x")
    scn.start()
    scn.wait_event("op_state")
    scn.control("abort", {"reason": "user cancelled"})
    result = scn.run()
    assert scn.ops.aborted == 1
    assert scn.bodies("op_state")[-1]["state"] == "killed"
    assert scn.bodies("tool_result")[0]["content"] == "error: killed"
    assert result == scn.bodies("settle")[0]
    assert result["outcome"] == "failed" and result["reason"] == "abort" and result["detail"] == "user cancelled"
    check_log(scn)


def test_abort_before_any_task_returns_without_a_settle_event():
    scn = Scenario()
    scn.control("abort", {"reason": "shutdown"})
    result = scn.run()
    assert result["outcome"] == "failed" and result["input_id"] is None
    assert scn.of("settle") == []
    check_log(scn)


def test_heartbeat_is_a_clock_event():
    scn = Scenario(script=[text("ok")])
    scn.control("heartbeat", {"at": T0 + 60_000})
    scn.task("x")
    scn.run()
    assert scn.bodies("clock")[1] == {"kind": "host_heartbeat", "at": T0 + 60_000}
    assert scn.coord.last_cseq == 2
    check_log(scn)


def test_redelivered_task_is_recorded_duplicate_and_runs_once():
    scn = Scenario(script=[text("ok")])
    scn.provider.hold = True
    scn.task("x", input_id="in-1")
    scn.task("x", input_id="in-1")
    scn.start()
    scn.wait_event("input", 2)
    scn.provider.release()
    scn.run()
    assert [b["dedupe"] for b in scn.bodies("input")] == ["new", "duplicate"]
    assert len(scn.of("turn_start")) == 1 and len(scn.provider.requests) == 1
    check_log(scn)


def test_follow_up_landing_mid_turn_wakes_another_turn():
    scn = Scenario(script=[text("first"), text("second")])
    scn.provider.hold = True
    scn.task("x", input_id="in-1")
    scn.start()
    scn.wait_event("turn_start")
    scn.task("and more", input_id="in-2", kind="follow_up")
    scn.wait_event("input", 2)
    scn.provider.release()
    result = scn.run()
    assert scn.bodies("wake") == [{"reason": "input", "result_ids": [], "input_ids": ["in-2"]}]
    assert len(scn.of("turn_start")) == 2
    r2 = scn.provider.requests[1]
    # the fold orders staged items by origin seq: the follow_up landed before the
    # response, so the model sees it before its own first answer
    assert [m["role"] for m in r2.messages] == ["system", "user", "user", "assistant"]
    assert r2.messages[2]["content"].endswith("and more")
    assert r2.messages[3]["content"] == "first"
    assert result["answer"] == "second" and result["input_id"] == "in-1"
    check_log(scn)


def test_input_during_a_tool_round_is_named_in_the_wake():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "x"})]), text("ok")], ops_manual=True)
    scn.task("x", input_id="in-1")
    scn.start()
    scn.wait_event("op_state")
    scn.task("steer!", input_id="in-2", kind="steer")
    scn.wait_event("input", 2)
    scn.ops.complete("c", "done")
    scn.run()
    assert scn.bodies("wake") == [{"reason": "tool_result", "result_ids": ["c"], "input_ids": ["in-2"]}]
    check_log(scn)


def test_second_hello_is_fatal():
    scn = Scenario()
    scn.control("hello", {"config": {}})
    scn.start()
    scn.thread.join(2)
    assert isinstance(scn.error, FatalProtocolError)
    assert scn.bodies("error") == [{"class": "fatal", "message": "second hello", "retry": False}]
    check_log(scn)


def test_park_is_logged_and_ignored_in_m1():
    scn = Scenario(script=[text("ok")])
    scn.control("park", {"reason": "idle"})
    scn.task("x")
    scn.run()
    assert any("park" in b["message"] for b in scn.transport.live_of("log"))
    check_log(scn)


def test_ack_items_mark_the_log():
    scn = Scenario(script=[text("ok")])
    scn.inbound.put(("ack", 3))
    scn.task("x")
    scn.run()
    assert scn.log.acked == 3 and scn.coord.acked_seq == 3


def test_link_down_defers_the_next_turn_until_link_up():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "x"})]), text("ok")], ops_manual=True)
    scn.task("x")
    scn.start()
    scn.wait_event("op_state")
    scn.inbound.put(("link", "down"))
    scn.ops.complete("c", "out")
    scn.wait_event("wake")
    time.sleep(0.05)
    assert len(scn.of("turn_start")) == 1
    scn.inbound.put(("link", "up"))
    scn.run()
    assert len(scn.of("turn_start")) == 2
    check_log(scn)


# ---------------------------------------------------------------- timers as clock events
def test_grace_expiry_is_recorded_as_a_clock_event():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "slow"})]), text("ok")], ops_manual=True)
    scn.task("x")
    scn.start()
    scn.wait_event("op_state")
    assert scn.clock.fire(GRACE_S) == 1
    scn.wait_event("clock", 3)
    assert scn.bodies("clock")[2]["kind"] == "grace_expiry"
    scn.ops.complete("c", "out")
    scn.run()
    assert len(scn.of("turn_start")) == 2
    check_log(scn)


def test_grace_is_cancelled_when_the_round_completes():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "x"})]), text("ok")], ops_results={"bash": "o"})
    scn.task("x")
    scn.run()
    assert scn.clock.pending(GRACE_S) == []
    assert [b["kind"] for b in scn.bodies("clock")] == ["host_hello", "turn_clock", "turn_clock"]


def test_idle_timer_is_a_clock_event_plus_log():
    scn = Scenario(script=[text("ok")])
    scn.start()
    scn.wait_for(lambda: scn.clock.pending(IDLE_S))
    scn.clock.fire(IDLE_S)
    scn.wait_event("clock", 2)
    assert scn.bodies("clock")[1]["kind"] == "idle_timer"
    assert any(b["message"] == "idle" for b in scn.transport.live_of("log"))
    scn.task("x")
    scn.run()
    check_log(scn)


def test_stale_timer_tokens_are_ignored():
    scn = Scenario(script=[text("ok")])
    scn.task("x")
    scn.inbound.put(("timer", "wall_clock", "wall:stale"))          # tokens that were never armed
    scn.inbound.put(("timer", "question_deadline", "question:nope"))
    scn.inbound.put(("timer", "op_timeout", "op:nope"))
    result = scn.run()
    assert [b["kind"] for b in scn.bodies("clock")] == ["host_hello", "turn_clock"]
    assert result["outcome"] == "done"
    check_log(scn)


# ---------------------------------------------------------------- provider errors
def test_provider_error_settles_failed():
    scn = Scenario(script=[ProviderError(True, 503, "upstream 503")])
    scn.task("x")
    result = scn.run()
    err = scn.bodies("error")[0]
    assert err["class"] == "transient" and err["retry"] is False and err["ref"] == scn.bodies("turn_start")[0]["turn_id"]
    assert result["outcome"] == "failed" and result["reason"] == "provider_error"
    check_log(scn)


def test_non_transient_provider_error_is_a_protocol_error():
    scn = Scenario(script=[ProviderError(False, 400, "bad request")])
    scn.task("x")
    scn.run()
    assert scn.bodies("error")[0]["class"] == "protocol"
    check_log(scn)


# ---------------------------------------------------------------- the invariant, in one place
def test_every_scenario_log_is_json_serialisable():
    scn = Scenario(script=[tool_calls([("c", "bash", {"command": "x"})]), text("ok")], ops_results={"bash": "o"})
    scn.task("x")
    scn.run()
    for e in scn.events:
        json.dumps(e)
    state = fold(scn.events)
    assert state_hash(state) == state_hash(scn.state)
