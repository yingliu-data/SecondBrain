"""Fakes for the host tests: a scripted guest speaking the vsock control
protocol over the session's Unix sockets, a Sandbox that runs it in a
thread, a fake registry of skills, and a scripted upstream LLM over TCP.

M3 (harness/docs/M3-CONTRACTS.md): the FakeGuest honours `park` (at a turn
boundary, or at once while it waits on a host-originated result): it emits
checkpoint + parked, waits for the acks, closes its socket and idle-waits
in memory until the FakeSandbox restores it from the session "snapshot"; it
then reconnects with a handshake ack of its cseq and emits nothing special.
A guest started for `hello{resume: true}` pulls the log return over :5001
(protocol.bulk get), continues the seq, marks open calls op_state{lost}
with an error result, and finishes the open task with `resume_text`.
`die=True` makes a guest die at its first host-originated call (or right
after a log return).
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from protocol.allowlist import effective_tools
from guest.bulk import BulkClient
from guest.transport import UnixConnector
from protocol.messages import ack, encode, parse
from protocol.sse import DONE_FRAME, response_chunks, sse_frame

from app.host.sandbox import Sandbox, SandboxError

HASH = "0" * 64
LOST_TEXT = "error: operation lost (VM restarted)"


class _Stopped(Exception):
    """The sandbox stopped the guest (also while it idle-waits after park)."""


class _Died(Exception):
    """The guest dies on purpose (die=True)."""


def ts_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def token_text(sse: list[str]) -> str:
    """The concatenated texts of the `token` events in projected SSE strings."""
    return "".join(json.loads(s.split("data: ", 1)[1])["text"]
                   for s in sse if s.startswith("event: token"))


class FakeGuest:
    """Connects to {ipc}/v.sock_5000, handshakes, waits for hello, then for
    every task runs `script`: a list of turns, each `{"text": ...}` (final
    answer) or `{"call": {"id", "name", "args"}}` (a tool call; side from
    hello's tools; host/device/mcp calls wait for side_result, guest calls
    get `guest_result`). Events are shaped like the goldens. A text turn
    may carry `deltas`: live `delta` fragments sent before the llm_response,
    of `delta_kind` ("content", default: M2 streaming; "text": the
    non-streamed whole-message delta), and `reasoning` fragments."""

    def __init__(self, script, *, guest_result="ok", exit_after_settle=True,
                 dedupe=True, seq_start=0, die=False, resume_text="resumed answer"):
        self.script = list(script)
        self.guest_result = guest_result
        self.exit_after_settle = exit_after_settle
        self.dedupe = dedupe
        self.sock: socket.socket | None = None
        self.hello: dict | None = None
        self.session_id: str | None = None
        self.seq = seq_start
        self.acked = 0
        self.highest_cseq = 0
        self.events: list[dict] = []
        self.controls: list[dict] = []
        self.seen_inputs: set[str] = set()
        self._side_results: queue.Queue = queue.Queue()
        self._tasks: queue.Queue = queue.Queue()
        self._ack_cv = threading.Condition()
        self._emit_lock = threading.Lock()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.turn_n = 0
        # M3
        self.die = die
        self.resume_text = resume_text
        self.ipc: Path | None = None
        self.parked = threading.Event()        # idle-waiting after parked
        self._restore = threading.Event()      # set by FakeSandbox.restore / stop
        self._park_req: str | None = None
        self._suspending = False
        self.parks = 0
        self.returned: list[dict] | None = None   # the log return, if any

    # ---------------------------------------------------------------- wire
    def connect(self, ipc: Path, port: int = 5000, timeout: float = 5.0) -> None:
        path = Path(ipc) / f"v.sock_{port}"
        deadline = time.monotonic() + timeout
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(path))
                self.sock = s
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    def send(self, msg: dict) -> None:
        self.sock.sendall(encode(msg).encode())

    def emit(self, type: str, body: dict) -> dict:
        with self._emit_lock:
            self.seq += 1
            ev = {"session_id": self.session_id, "seq": self.seq, "ts": ts_now(),
                  "schema": 1, "type": type, "body": body}
            self.events.append(ev)
            self.send(ev)
            return ev

    def live(self, type: str, body: dict) -> None:
        self.send({"type": type, "body": body})

    def delta(self, turn_id: str, kind: str, text: str, index: int = 0) -> None:
        self.live("delta", {"session_id": self.session_id, "turn_id": turn_id,
                            "kind": kind, "index": index, "text": text})

    def wait_acked(self, seq: int, timeout: float = 5.0) -> bool:
        with self._ack_cv:
            return self._ack_cv.wait_for(lambda: self.acked >= seq, timeout)

    def _reader(self) -> None:
        try:
            with self.sock.makefile("rb") as f:
                for raw in f:
                    kind, msg = parse(raw)
                    if kind == "ack":
                        with self._ack_cv:
                            self.acked = max(self.acked, msg.get("ack_seq", 0))
                            self._ack_cv.notify_all()
                    elif kind == "control":
                        n = msg["cseq"]
                        if msg["type"] == "hello" and self.hello is None:
                            pass                # a fresh inbox takes hello's cseq as next
                        elif n <= self.highest_cseq:
                            self.send(ack(msg["session_id"], ack_cseq=self.highest_cseq))
                            continue            # duplicate (a resend)
                        elif n != self.highest_cseq + 1:
                            continue            # gap: wait for the resend
                        self.controls.append(msg)
                        self.highest_cseq = n
                        self.send(ack(msg["session_id"], ack_cseq=self.highest_cseq))
                        if msg["type"] == "hello":
                            self._tasks.put(msg)
                        elif msg["type"] == "park":
                            self._park_req = msg["body"]["reason"]
                            self._tasks.put(("park",))
                            self._side_results.put(("park",))
                        elif msg["type"] == "ui_response":
                            pass                # recorded in self.controls only
                        elif msg["type"] in ("task", "follow_up", "steer", "ui_response"):
                            iid = msg["body"]["input_id"]
                            if self.dedupe and iid in self.seen_inputs:
                                # the inbox classifies at once, mid-task too
                                self.emit("input", {"input_id": iid, "kind": msg["type"],
                                                    "text": msg["body"].get("text", ""),
                                                    "dedupe": "duplicate"})
                            else:
                                self._tasks.put(msg)
                        elif msg["type"] == "side_result":
                            self._side_results.put(msg["body"])
        except (OSError, ValueError):
            pass
        finally:
            if not self._suspending:
                self._tasks.put(None)

    # ---------------------------------------------------------------- park
    def _do_park(self) -> None:
        """M3-CONTRACTS §3: checkpoint, parked, wait acked, close, idle-wait;
        on restore reconnect and handshake. Keeps every in-memory counter."""
        reason, self._park_req = self._park_req, None
        cp = self.emit("checkpoint", {"through_seq": self.seq, "state_hash": HASH,
                                      "prev_hash": HASH})
        p = self.emit("parked", {"checkpoint_seq": cp["seq"], "reason": reason})
        self.wait_acked(p["seq"])
        self._suspending = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except OSError:
            pass
        self.parks += 1
        self.parked.set()
        self._restore.wait()
        self._restore.clear()
        if self._stop.is_set():
            raise _Stopped()
        self.parked.clear()
        self._suspending = False
        self.connect(self.ipc)
        self.send(ack(self.session_id, ack_cseq=self.highest_cseq))   # the handshake
        threading.Thread(target=self._reader, daemon=True).start()

    def _maybe_park(self) -> None:
        if self._park_req is not None:
            self._do_park()

    # ---------------------------------------------------------------- run
    def run(self, ipc: Path) -> None:
        self.ipc = Path(ipc)
        try:
            self.connect(ipc)
            threading.Thread(target=self._reader, daemon=True).start()
            while not self._stop.is_set():
                msg = self._tasks.get()
                if msg is None:
                    return
                if isinstance(msg, tuple):          # a park marker
                    self._maybe_park()
                    continue
                if msg["type"] == "hello":
                    self._on_hello(msg)
                    continue
                if self.hello is None:
                    continue
                if msg["type"] == "_resume":
                    done = self._finish_resumed(msg["body"]["input_id"])
                else:
                    done = self._on_input(msg)
                if done and self.exit_after_settle:
                    return
        except (_Stopped, _Died):
            pass
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)   # the reader holds a ref: shutdown, not close
                self.sock.close()
            except Exception:
                pass

    def _on_hello(self, msg: dict) -> None:
        self.hello = msg["body"]
        self.session_id = self.hello["session_id"]
        self.tools = effective_tools(self.hello["config"])
        p = self.hello["principal"]
        if self.hello.get("resume") and self.seq == 0:
            self._log_return()
            self.live("ready", {"at": int(time.time() * 1000)})
            return
        if self.seq == 0:
            self.emit("session_start", {
                "user": p["user"], "tenant": p["tenant"],
                "workflow_run_id": f"{p['user']}/{p['tenant']}/{self.session_id}",
                "config_hash": HASH, "system_hash": HASH, "tools": self.tools,
                "harness_version": "fake", "image_version": "fake",
                "resumed": bool(self.hello.get("resume")),
            })
        self.live("ready", {"at": int(time.time() * 1000)})

    def _log_return(self) -> None:
        """M3-CONTRACTS §4.4: bulk get of the acked log, fold, op_state{lost}."""
        events, self.seq = BulkClient(UnixConnector(self.ipc), self.session_id).get_log(0)
        self.returned = events
        settled = {e["body"]["input_id"] for e in events if e["type"] == "settle"}
        open_input = None
        calls: dict[str, bool] = {}
        for e in events:
            t, b = e["type"], e["body"]
            if t == "input" and b["dedupe"] == "new":
                self.seen_inputs.add(b["input_id"])
                if b["input_id"] not in settled:
                    open_input = b["input_id"]
            elif t == "turn_start":
                self.turn_n += 1
            elif t == "tool_call":
                calls[b["call_id"]] = True
            elif t == "tool_result":
                calls[b["call_id"]] = False
        lost = [cid for cid, is_open in calls.items() if is_open]
        for cid in lost:
            self.emit("op_state", {"call_id": cid, "state": "lost"})
            self.emit("tool_result", {"call_id": cid, "content": LOST_TEXT})
        if lost:
            self.emit("wake", {"reason": "tool_result", "result_ids": lost, "input_ids": []})
        if self.die:
            raise _Died()
        if open_input is not None:
            self._tasks.put({"type": "_resume", "body": {"input_id": open_input}})

    def _finish_resumed(self, input_id: str) -> bool:
        self.turn_n += 1
        turn_id = f"turn_{self.turn_n:04d}"
        self.emit("turn_start", {
            "turn_id": turn_id, "type": "regular", "includes_through_seq": self.seq,
            "model": self.hello["config"]["model"], "params": self.hello["config"]["params"],
            "request_hash": HASH, "preamble_hash": HASH, "workspace_tree_id": None,
        })
        self.emit("llm_response", {
            "turn_id": turn_id, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "message": {"role": "assistant", "content": self.resume_text},
        })
        settle = self.emit("settle", {
            "input_id": input_id, "outcome": "done", "answer": self.resume_text,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "manifest": {},
        })
        self.wait_acked(settle["seq"])
        return True

    def _side(self, name: str) -> str:
        for t in self.tools:
            if t["name"] == name:
                return t["side"]
        return "guest"

    def _on_input(self, msg: dict) -> bool:
        body = msg["body"]
        input_id = body["input_id"]
        kind = msg["type"]
        if self.dedupe and input_id in self.seen_inputs:
            self.emit("input", {"input_id": input_id, "kind": kind,
                                "text": body.get("text", ""), "dedupe": "duplicate"})
            return False
        self.seen_inputs.add(input_id)
        self.emit("input", {"input_id": input_id, "kind": kind,
                            "text": body.get("text", ""), "dedupe": "new"})
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        answer = ""
        for turn in self.script:
            self._maybe_park()                  # a turn boundary
            self.turn_n += 1
            turn_id = f"turn_{self.turn_n:04d}"
            self.emit("clock", {"kind": "turn_clock", "at": int(time.time() * 1000)})
            self.emit("turn_start", {
                "turn_id": turn_id, "type": "regular", "includes_through_seq": self.seq,
                "model": self.hello["config"]["model"], "params": self.hello["config"]["params"],
                "request_hash": HASH, "preamble_hash": HASH, "workspace_tree_id": None,
            })
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            usage_total["prompt_tokens"] += 10
            usage_total["completion_tokens"] += 5
            if "text" in turn:
                answer = turn["text"]
                for d in turn.get("reasoning", []):
                    self.delta(turn_id, "reasoning", d)
                for d in turn.get("deltas", []):
                    self.delta(turn_id, turn.get("delta_kind", "content"), d)
                self.emit("llm_response", {
                    "turn_id": turn_id, "finish_reason": "stop", "usage": usage,
                    "message": {"role": "assistant", "content": turn["text"]},
                })
                continue
            call = turn["call"]
            side = self._side(call["name"])
            self.emit("llm_response", {
                "turn_id": turn_id, "finish_reason": "tool_calls", "usage": usage,
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": call["id"], "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                }]},
            })
            self.emit("guard_decision", {"ref": call["id"], "decision": "allow",
                                         "reason": "allowlist", "policy_id": "pass-through"})
            self.emit("tool_call", {"turn_id": turn_id, "call_id": call["id"],
                                    "name": call["name"], "side": side,
                                    "args": call["args"], "state": "RUNNING"})
            self.emit("op_state", {"call_id": call["id"], "state": "running"})
            if side == "guest":
                content, ok = self.guest_result, True
            else:
                if self.die:
                    raise _Died()           # the VM dies mid host-originated call
                while True:
                    res = self._side_results.get(timeout=turn.get("wait_s", 10))
                    if isinstance(res, tuple):  # park while only host ops run: at once
                        self._maybe_park()
                        continue
                    break
                content, ok = res["content"], res["ok"]
            self.emit("op_state", {"call_id": call["id"], "state": "done" if ok else "failed",
                                   "duration_ms": 1})
            self.emit("tool_result", {"call_id": call["id"], "content": content})
            self.emit("wake", {"reason": "tool_result", "result_ids": [call["id"]],
                               "input_ids": []})
        settle = self.emit("settle", {
            "input_id": input_id, "outcome": "done", "answer": answer,
            "usage": usage_total, "manifest": {},
        })
        self.wait_acked(settle["seq"])
        return True


class FakeSandbox(Sandbox):
    """Runs a FakeGuest in a thread for every start(); a new FakeGuest
    (fresh seq) per run, like a fresh guest process.

    M3: `base=True` offers a base snapshot (restore(dir, None) spawns a new
    guest, like a restore of the idle image); snapshot() waits for the guest
    to idle-wait after `parked` and keeps it as the session snapshot (META.json
    in snapshot_dir); restore(dir, snapshot_dir) wakes that same guest.
    The first `die_runs` guests get die=True."""

    def __init__(self, script, *, base: bool = False, die_runs: int = 0,
                 restore_delay: float = 0.0, **guest_kwargs):
        self.script = script
        self.guest_kwargs = guest_kwargs
        self.guests: list[FakeGuest] = []
        self.guest: FakeGuest | None = None
        self.starts = 0
        self.base = base
        self.die_runs = die_runs
        self.restore_delay = restore_delay
        self.restores: list[str] = []
        self.snapshots: list[str] = []
        self._parked_guest: FakeGuest | None = None

    def _spawn(self, session_dir: Path) -> None:
        kwargs = dict(self.guest_kwargs)
        if len(self.guests) < self.die_runs:
            kwargs["die"] = True
        g = FakeGuest(self.script, **kwargs)
        g.thread = threading.Thread(target=g.run, args=(Path(session_dir) / "ipc",), daemon=True)
        g.thread.start()
        self.guest = g
        self.guests.append(g)

    def start(self, session_dir: Path) -> None:
        if self.is_alive():
            return
        self.starts += 1
        self._spawn(session_dir)

    def restore(self, session_dir: Path, snapshot_dir: Path | None = None) -> None:
        if self.restore_delay:
            time.sleep(self.restore_delay)
        if snapshot_dir is None:
            if not self.base:
                raise SandboxError("no base snapshot")
            self.restores.append("base")
            self._spawn(session_dir)
            return
        g = self._parked_guest
        meta = Path(snapshot_dir) / "META.json"
        if g is None or not meta.exists():
            raise SandboxError("no session snapshot")
        meta.unlink()              # a session snapshot is consumed by its restore
        self._parked_guest = None
        self.restores.append("session")
        self.guest = g
        g._restore.set()

    def snapshot(self, snapshot_dir: Path) -> None:
        g = self.guest
        if g is None or not g.parked.wait(5):
            raise RuntimeError("guest is not parked")
        Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
        (Path(snapshot_dir) / "META.json").write_text(json.dumps({"seq": g.seq}))
        self.snapshots.append(str(snapshot_dir))
        self._parked_guest = g
        self.guest = None          # the VM is stopped; its memory is the snapshot

    def has_snapshot(self, snapshot_dir: Path | None = None) -> bool:
        if snapshot_dir is None:
            return self.base
        return self._parked_guest is not None and (Path(snapshot_dir) / "META.json").exists()

    def stop(self) -> None:
        g = self.guest
        if g is not None:
            g._stop.set()
            g._restore.set()
            try:
                if g.sock:
                    g.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if g.thread:
                g.thread.join(timeout=2)
        self.guest = None

    def is_alive(self) -> bool:
        return self.guest is not None and self.guest.thread is not None and self.guest.thread.is_alive()


class FakeRegistry:
    """Two host tools and one device tool; records executions."""

    def __init__(self, results: dict | None = None):
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    def _defs(self):
        return [
            {"type": "function", "function": {"name": "web_search", "description": "search",
                                              "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "set_pose", "description": "avatar",
                                              "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "get_calendar_events",
                                              "description": "device",
                                              "parameters": {"type": "object", "properties": {}}}},
        ]

    def get_active_tool_definitions(self, allowed=None, engine=None):
        return self._defs()

    def get_tools_for_query(self, query, allowed=None):
        return self._defs()

    def get_server_tool_names(self, allowed=None, engine=None):
        return {"web_search", "set_pose"}

    def get_device_tool_names(self, allowed=None, engine=None):
        return {"get_calendar_events"}

    async def execute_server_tool(self, name, arguments, allowed=None, engine=None):
        self.calls.append((name, arguments))
        r = self.results.get(name, f"{name} result")
        if isinstance(r, Exception):
            raise r
        return r


class FakeTenant:
    def __init__(self, name="t", user="u", engine=None, system_prompt=None,
                 local_skills=None, max_tools=None, max_tokens=None):
        self.name, self.user, self.engine = name, user, engine
        self.system_prompt = system_prompt
        self.local_skills = local_skills
        self.max_tools, self.max_tokens = max_tools, max_tokens

    def allowed_skill_names(self):
        return None if self.local_skills is None else set(self.local_skills)


class FakeUpstreamLLM:
    """A scripted OpenAI-style chat-completions server on 127.0.0.1 (TCP, so
    httpx can reach it). `responses` is a list of assistant messages (dict
    with content and/or tool_calls) handed out in order; the last repeats.

    `status != 200` answers that status with `error_body` (default
    `{"error": "nope"}`). A request with `stream: true` is served as
    text/event-stream in chunked HTTP/1.1 the way vLLM streams it (mirrors
    harness/tests/fakes/fake_llm.py; both use protocol.sse.response_chunks):
    a role chunk, content split into words, each tool call as an indexed
    first fragment (id, name, first piece of the arguments) plus argument
    fragments, a finish_reason chunk, a usage-only chunk, `[DONE]`. `keepalive=True` puts a `: keep-alive` comment before each
    chunk; `cut_after=n` closes the connection after n data chunks (no
    finish_reason, no [DONE]). `streamed` keeps the SSE text sent per streamed
    call, for verbatim checks."""

    def __init__(self, responses, status=200):
        self.responses = list(responses)
        self.status = status
        self.error_body: dict | None = None
        self.keepalive = False
        self.cut_after: int | None = None
        self.requests: list[dict] = []
        self.streamed: list[str] = []
        self.n = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(req)
                if outer.status != 200:
                    body = json.dumps(outer.error_body if outer.error_body is not None
                                      else {"error": "nope"}).encode()
                    self.send_response(outer.status)
                else:
                    i = min(outer.n, len(outer.responses) - 1)
                    outer.n += 1
                    msg = outer.responses[i]
                    finish = "tool_calls" if msg.get("tool_calls") else "stop"
                    full = {
                        "id": f"chatcmpl-fake-{outer.n}", "object": "chat.completion",
                        "model": req.get("model", "fake"),
                        "choices": [{"index": 0, "message": {"role": "assistant", **msg},
                                     "finish_reason": finish}],
                        "usage": {"prompt_tokens": 100 + outer.n, "completion_tokens": 7,
                                  "total_tokens": 107 + outer.n},
                    }
                    if req.get("stream") is True:
                        self._stream(full)
                        return
                    body = json.dumps(full).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, full: dict) -> None:
                writes: list[bytes] = []
                cut = False
                for n, chunk in enumerate(response_chunks(full), start=1):
                    if outer.cut_after is not None and n > outer.cut_after:
                        cut = True
                        break
                    if outer.keepalive:
                        writes.append(b": keep-alive\n\n")
                    writes.append(sse_frame(json.dumps(chunk, separators=(",", ":"))))
                if not cut:
                    writes.append(DONE_FRAME)
                outer.streamed.append(b"".join(writes).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for data in writes:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                if cut:
                    self.close_connection = True
                else:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------- fixtures
import pytest  # noqa: E402

from app.session.session_dir import SessionDir  # noqa: E402


@pytest.fixture
def users_root(tmp_path, monkeypatch):
    root = tmp_path / "users"
    monkeypatch.setattr("app.user.context_builder.USERS_ROOT", str(root))
    monkeypatch.setattr("app.user.profile.USERS_ROOT", str(root))
    return root


@pytest.fixture
def session(tmp_path, users_root):
    return SessionDir.create(tmp_path / "u" / "t_s1", user_id="u", session_id="s1", tenant="t")


def worker_kwargs(**over):
    base = dict(hello_timeout_s=5, heartbeat_s=0.2, wall_clock_s=10)
    base.update(over)
    return base
