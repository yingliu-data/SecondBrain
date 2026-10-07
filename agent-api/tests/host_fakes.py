"""Fakes for the host tests: a scripted guest speaking the vsock control
protocol over the session's Unix sockets, a Sandbox that runs it in a
thread, a fake registry of skills, and a scripted upstream LLM over TCP.
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
from protocol.messages import ack, encode, parse

from app.host.sandbox import Sandbox

HASH = "0" * 64


def ts_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FakeGuest:
    """Connects to {ipc}/v.sock_5000, handshakes, waits for hello, then for
    every task runs `script`: a list of turns, each `{"text": ...}` (final
    answer) or `{"call": {"id", "name", "args"}}` (a tool call; side from
    hello's tools; host/device/mcp calls wait for side_result, guest calls
    get `guest_result`). Events are shaped like the goldens."""

    def __init__(self, script, *, guest_result="ok", exit_after_settle=True,
                 dedupe=True, seq_start=0):
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
                        self.controls.append(msg)
                        self.highest_cseq = max(self.highest_cseq, msg["cseq"])
                        self.send(ack(msg["session_id"], ack_cseq=self.highest_cseq))
                        if msg["type"] == "hello":
                            self._tasks.put(msg)
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
            self._tasks.put(None)

    # ---------------------------------------------------------------- run
    def run(self, ipc: Path) -> None:
        try:
            self.connect(ipc)
            threading.Thread(target=self._reader, daemon=True).start()
            while not self._stop.is_set():
                msg = self._tasks.get()
                if msg is None:
                    return
                if msg["type"] == "hello":
                    self._on_hello(msg)
                    continue
                if self.hello is None:
                    continue
                done = self._on_input(msg)
                if done and self.exit_after_settle:
                    return
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
        if self.seq == 0:
            self.emit("session_start", {
                "user": p["user"], "tenant": p["tenant"],
                "workflow_run_id": f"{p['user']}/{p['tenant']}/{self.session_id}",
                "config_hash": HASH, "system_hash": HASH, "tools": self.tools,
                "harness_version": "fake", "image_version": "fake",
                "resumed": bool(self.hello.get("resume")),
            })
        self.live("ready", {"at": int(time.time() * 1000)})

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
                if turn.get("deltas"):
                    for d in turn["deltas"]:
                        self.live("delta", {"turn_id": turn_id, "kind": "text", "text": d})
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
                res = self._side_results.get(timeout=turn.get("wait_s", 10))
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
    (fresh seq) per run, like a fresh guest process."""

    def __init__(self, script, **guest_kwargs):
        self.script = script
        self.guest_kwargs = guest_kwargs
        self.guests: list[FakeGuest] = []
        self.guest: FakeGuest | None = None
        self.starts = 0

    def start(self, session_dir: Path) -> None:
        if self.is_alive():
            return
        self.starts += 1
        g = FakeGuest(self.script, **self.guest_kwargs)
        g.thread = threading.Thread(target=g.run, args=(Path(session_dir) / "ipc",), daemon=True)
        g.thread.start()
        self.guest = g
        self.guests.append(g)

    def stop(self) -> None:
        g = self.guest
        if g is not None:
            g._stop.set()
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

    def get_active_tool_definitions(self, allowed=None):
        return self._defs()

    def get_tools_for_query(self, query, allowed=None):
        return self._defs()

    def get_server_tool_names(self, allowed=None):
        return {"web_search", "set_pose"}

    def get_device_tool_names(self, allowed=None):
        return {"get_calendar_events"}

    async def execute_server_tool(self, name, arguments, allowed=None):
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
    with content and/or tool_calls) handed out in order; the last repeats."""

    def __init__(self, responses, status=200):
        self.responses = list(responses)
        self.status = status
        self.requests: list[dict] = []
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
                    body = json.dumps({"error": "nope"}).encode()
                    self.send_response(outer.status)
                else:
                    i = min(outer.n, len(outer.responses) - 1)
                    outer.n += 1
                    msg = outer.responses[i]
                    finish = "tool_calls" if msg.get("tool_calls") else "stop"
                    body = json.dumps({
                        "id": f"chatcmpl-fake-{outer.n}", "object": "chat.completion",
                        "model": req.get("model", "fake"),
                        "choices": [{"index": 0, "message": {"role": "assistant", **msg},
                                     "finish_reason": finish}],
                        "usage": {"prompt_tokens": 100 + outer.n, "completion_tokens": 7,
                                  "total_tokens": 107 + outer.n},
                    }).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
