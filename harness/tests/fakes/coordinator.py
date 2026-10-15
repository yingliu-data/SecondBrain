"""Coordinator test fakes: a manual clock, a recording transport and log
writer, scripted guest operations and provider, a real GuestConfig built from
a hello body (`make_cfg`), and `Scenario`, which drives a Coordinator on a
thread and plays the host. `check_log` is the core invariant (the log
validates, replays and folds to the coordinator's own State).

Shared by test_guest_coordinator, test_guest_m3_units, test_guest_m5,
test_guest_async and the TaskView tests.
"""
from __future__ import annotations

import json
import queue
import threading
import time

from guest.config import GuestConfig, from_hello
from guest.coordinator import Coordinator
from guest.provider import ProviderError
from protocol.events import validate_log
from protocol.fold import State, replay, state_hash
from tests.fakes.fake_llm import response, text, tool_calls  # noqa: F401  (re-exported)

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
        self.wire: list = []          # (type, body) of events and live messages, in send order
        self.parks = 0
        self._lock = threading.Lock()

    def send_event(self, event):
        with self._lock:
            self.events.append(event)
            self.wire.append((event["type"], event["body"]))
        for fn in list(self.on_event):
            fn(event)

    def send_live(self, type, body):
        with self._lock:
            self.live.append((type, body))
            self.wire.append((type, body))

    def send_ack(self, cseq):
        self.acks.append(cseq)

    # park: every event counts as acked at once; park() is recorded
    def wait_acked(self, seq, timeout=None):
        return True

    @property
    def acked_seq(self):
        return self.events[-1]["seq"] if self.events else 0

    def park(self):
        self.parks += 1

    def park_on_ack(self, seq):
        self.parks += 1               # every event counts as acked at once

    def live_of(self, type):
        return [b for t, b in self.live if t == type]


class FakeLogWriter:
    def __init__(self, transport):
        self.transport = transport
        self.events: list = []
        self.acked = 0
        self.synced = 0

    def put(self, event):
        self.events.append(event)
        self.transport.send_event(event)

    def mark_acked(self, seq):
        self.acked = max(self.acked, seq)

    def sync_acked(self):
        self.synced = self.acked

    def flush(self, timeout=None):
        return True


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
        self.mcp_submitted: list = []
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

    def workspace_tree_id(self):
        """No workspace in these scenarios: turn_start.workspace_tree_id is None."""
        return None

    # mcp surface (OperationManager's): an mcp call is an op like any other here
    def submit_mcp(self, call_id, name, args, *, attempt=0, input_response=None, timeout_s=None,
                   reattach=False):
        self.mcp_submitted.append((call_id, name, args, attempt, input_response, reattach))
        self.submit(call_id, name, args)

    def detach_mcp(self):
        return set()

    def cancel(self, call_id):
        self._running.discard(call_id)

    def send_mcp(self, call_id, name, args, *, attempt, input_response=None):
        pass


def stream_fragments(response) -> list:
    """The fragments guest.provider hands on_delta for `response` served as
    SSE: the real assembly pipeline over the chunks the fake LLM streams."""
    from guest.provider import assemble_chunks
    from protocol.sse import response_chunks
    out: list = []
    assemble_chunks(response_chunks(response), out.append)
    return out


class Streamed:
    """A script entry that streams: on_delta gets each fragment (a dict
    {kind, index, text} or a plain str = a content fragment), then the
    assembled response is returned, as guest.provider does with stream=True
    (de-dup across its retries is the provider's business). Fragments
    default to what the real pipeline yields for the response."""

    def __init__(self, response, fragments=None):
        self.response = response
        if fragments is None:
            fragments = stream_fragments(response)
        self.fragments = [f if isinstance(f, dict) else {"kind": "content", "index": 0, "text": f}
                          for f in fragments]


def overflow_error(message="upstream 400: This model's maximum context length is 32768 tokens"):
    exc = ProviderError(False, 400, message)
    exc.overflow = True
    return exc


def budget_error():
    exc = ProviderError(False, 402, "upstream 402: token budget exhausted")
    exc.budget = True
    return exc


class FakeProvider:
    """Scripted responses with guest.provider's signature
    chat_completion(request, *, stream=False, on_delta=None); an entry may be
    a response dict, a Streamed, a callable(request) or an Exception to raise.
    With stream=True a Streamed entry's fragments go to on_delta; with
    stream=False on_delta gets one {kind: "text"} fragment with the whole
    content, as the provider sends it. `hold` blocks each call until
    `release()`. `calls` records (stream, on_delta is not None) per call."""

    def __init__(self, script=(), hold_at=()):
        self.script = list(script)
        self.requests: list = []
        self.calls: list = []
        self.hold = False
        self.hold_at = set(hold_at)      # 1-based call numbers to hold until release()
        self._gate = threading.Event()
        self._gate.set()

    def push(self, *entries):
        self.script.extend(entries)

    def release(self):
        self._gate.set()

    def chat_completion(self, request, *, stream=False, on_delta=None):
        self.requests.append(request)
        self.calls.append((stream, on_delta is not None))
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
        fragments = []
        if isinstance(entry, Streamed):
            fragments, entry = entry.fragments, entry.response
        if on_delta is not None:
            if not stream:
                content = (entry.get("choices") or [{}])[0].get("message", {}).get("content")
                fragments = [{"kind": "text", "index": 0, "text": content}] if content else []
            for fragment in fragments:
                on_delta(dict(fragment))
        return entry


# ---------------------------------------------------------------- config
LIMIT_KEYS = ("max_tools", "max_turns", "spill_threshold", "context_window_tokens", "reserve_tokens",
              "grace_s", "answer_wait_s", "park_grace_s", "wall_clock_s")
CONFIG_KEYS = ("async_tools", "question_timeout_s", "ui_attached", "params", "model")
BODY_KEYS = ("session_id", "resume", "at", "system_block")


def make_cfg(extra_tools=(), **over) -> GuestConfig:
    """A real GuestConfig, parsed from a hello body by guest.config.from_hello
    (so every default is the parser's). `over` names limits, config or body
    keys (LIMIT_KEYS, CONFIG_KEYS, BODY_KEYS)."""
    limits = {"wall_clock_s": int(WALL_S), "max_tools": 10, "max_turns": 12, "max_tokens": 256,
              "op_timeout_s": dict(OP_TIMEOUTS), "spill_threshold": 2000, "grace_s": GRACE_S,
              "idle_wake_s": int(IDLE_S), "context_window_tokens": 32768, "reserve_tokens": 4096}
    config = {"model": "fake-model", "params": {"temperature": 0}, "tools": list(extra_tools),
              "limits": limits, "async_tools": False, "question_timeout_s": int(QUESTION_S),
              "ui_attached": True}
    body = {"session_id": SID, "principal": {"user": USER, "tenant": TENANT}, "config": config,
            "system_block": {"base_prompt": "You are a test.", "profile": "", "memory_index": ""},
            "resume": False, "at": T0}
    for key, value in over.items():
        if key in LIMIT_KEYS:
            limits[key] = value
        elif key in CONFIG_KEYS:
            config[key] = value
        elif key in BODY_KEYS:
            body[key] = value
        else:
            raise TypeError(f"make_cfg: unknown override {key!r}")
    return from_hello(body)


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


def view_snapshot(view) -> dict:
    """A TaskView's fields, comparable across coordinators (a side call's
    started_ms is the guest clock when its event was applied)."""
    out = {k: v for k, v in vars(view).items() if k not in ("cfg", "_now_ms", "_async")}
    out["side_calls"] = {cid: {k: v for k, v in sc.items() if k != "started_ms"}
                         for cid, sc in view.side_calls.items()}
    return json.loads(json.dumps(out, default=sorted, sort_keys=True))


def restored_view(cfg, events):
    """The TaskView a fresh coordinator rebuilds from `events` (restore())."""
    inbound = queue.Queue()
    transport = FakeTransport()
    coord = Coordinator(cfg, State(), inbound, FakeLogWriter(transport), transport,
                        FakeProvider(), FakeOps(inbound), FakeClock())
    coord.restore(events)
    return coord.view


def view_problems(scn) -> list[str]:
    """One reducer: the live coordinator's TaskView equals the one restore()
    rebuilds from its log. [] when it does (or there is nothing to compare)."""
    events = scn.events
    if not events or events[0]["type"] != "session_start":
        return []
    live = view_snapshot(scn.coord.view)
    rebuilt = view_snapshot(restored_view(scn.cfg, events))
    return [f"TaskView.{k}: live {live[k]!r} != restored {rebuilt.get(k)!r}"
            for k in live if live[k] != rebuilt.get(k)]


BIG = "x" * 1500
LONG_TASK = "cat the big file and tell me what is in it. " * 4


def sized(entry):
    """A script entry whose usage.prompt_tokens is the raw estimate of the
    request it answers (a well-behaved server), so the correction stays ~1."""
    from guest.context import estimate_tokens

    def fn(request):
        out = json.loads(json.dumps(entry))
        n = estimate_tokens(request)
        out["usage"] = {"prompt_tokens": n, "completion_tokens": 10, "total_tokens": n + 10}
        return out
    return fn
