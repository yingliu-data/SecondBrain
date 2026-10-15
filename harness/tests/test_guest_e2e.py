"""End to end: guest.main as a subprocess over Unix-socket doubles of the
vsock ports, driven by FakeHost (:5000) and FakeLLM (:5010).

Scenarios: a text answer (streamed: the fake serves SSE, the guest sends a
live delta per fragment); the same with `params.stream: false` (one whole-
text delta, sent by the provider); a cut stream retried by the provider
(each fragment reaches the host once); `bash` echo then answer; a device
tool answered by the fake host's side_results; a host-side `remember` whose
side_result names the memory fact; a redelivered task (the same input_id twice, on the wire
and as a fresh control) runs one turn; a dropped link mid-turn produces no
duplicate events and the log replays; a 40-turn session with a window
small enough to compact several times, whose log replays and whose prefix
stays bounded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import guest.ops
import guest.transport
from protocol.events import validate_log
from protocol.fold import fold, replay, state_hash
from protocol.ports import PORTS
from tests.fakes.fake_host import FakeHost
from tests.fakes.fake_llm import FakeLLM, text, tool_calls

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "sess-e2e"
USER, TENANT = "e2e-user", "default"
T0 = 1_790_000_000_000


def hello_body(extra_tools=(), **config_over) -> dict:
    config = {
        "model": "fake-model",
        "params": {"temperature": 0},
        "tools": list(extra_tools),
        "limits": {"wall_clock_s": 60, "max_tools": 10, "max_turns": 12, "max_tokens": 128,
                   "op_timeout_s": {"device": 5, "host": 5}, "grace_s": 0.2, "idle_wake_s": 600},
        "async_tools": False,
        "question_timeout_s": 5,
        "ui_attached": True,
        "ports": dict(PORTS),
    }
    config.update(config_over)
    return {
        "config": config,
        "session_id": SID,
        "principal": {"user": USER, "tenant": TENANT},
        "system_block": {"base_prompt": "You are a test assistant. {current_time}",
                         "profile": "", "memory_index": ""},
        "resume": False,
        "at": T0,
    }


class Guest:
    """The guest process plus its fakes."""

    def __init__(self, tmp_path, hello, script=(), side_results=None):
        self.dir = str(tmp_path / "sock")
        self.log_dir = str(tmp_path / "log")
        self.workspace = str(tmp_path / "workspace")
        for d in (self.dir, self.log_dir, self.workspace):
            os.makedirs(d, exist_ok=True)
        self.llm = FakeLLM(self.dir, list(script))
        self.host = FakeHost(self.dir, hello, side_results)
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Guest":
        self.llm.start()
        self.host.start()
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "guest.main", "--connect", f"unix:{self.dir}",
             "--log-dir", self.log_dir, "--workspace", self.workspace,
             "--hello-timeout", "10", "--image-version", "e2e"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # the fake host writes only to a live connection: wait for the guest
        # to connect and ack the hello before a test sends anything
        self.host.wait_connected(timeout=10)
        self.host.wait_acked_cseq(1, timeout=10)
        return self

    def finish(self, timeout=15.0) -> int:
        """End the session the way a host stop does (abort: the guest stays
        up after a settle otherwise) and collect the exit code."""
        assert self.proc is not None
        if self.proc.poll() is None and self.host.connected:
            self.host.send("abort", {"reason": "test over"})
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
            raise AssertionError("guest did not exit:\n" + err.decode("utf-8", "replace"))
        self.stderr = err.decode("utf-8", "replace")
        return self.proc.returncode

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.communicate()
        self.host.stop()
        self.llm.stop()

    def send_task(self, text_="hello", input_id="in-1", kind="task") -> dict:
        return self.host.send(kind, {"input_id": input_id, "text": text_, "attachments": []})

    @property
    def events(self) -> list:
        return list(self.host.events)

    def types(self) -> list:
        return [e["type"] for e in self.events]

    def of(self, type) -> list:
        return [e["body"] for e in self.events if e["type"] == type]

    def local_log(self) -> list:
        """The guest's own buffered segments, in seq order."""
        events = []
        for name in sorted(os.listdir(self.log_dir)):
            path = os.path.join(self.log_dir, name)
            if name.endswith(".jsonl") and os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            events.append(json.loads(line))
        for name in sorted(os.listdir(self.log_dir)):
            sub = os.path.join(self.log_dir, name)
            if os.path.isdir(sub):
                for inner in sorted(os.listdir(sub)):
                    if inner.endswith(".jsonl"):
                        with open(os.path.join(sub, inner), encoding="utf-8") as fh:
                            for line in fh:
                                if line.strip():
                                    events.append(json.loads(line))
        return sorted(events, key=lambda e: e["seq"])


def check_replay(events: list):
    assert validate_log(events) == []
    state, errors = replay(events)
    assert errors == []
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    return state


@pytest.fixture
def guest_factory(tmp_path):
    made = []

    def make(hello, script=(), side_results=None) -> Guest:
        g = Guest(tmp_path, hello, script, side_results).start()
        made.append(g)
        return g

    yield make
    for g in made:
        g.stop()


# ---------------------------------------------------------------- scenarios
def test_text_answer(guest_factory):
    g = guest_factory(hello_body(), script=[text("2 + 2 = 4.")])
    g.send_task("What is 2 + 2?")
    settle = g.host.wait_for("settle", timeout=10)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "2 + 2 = 4." and settle["input_id"] == "in-1"
    assert g.types() == ["session_start", "clock", "input", "clock", "turn_start", "llm_response", "settle"]
    start = g.of("session_start")[0]
    assert start["user"] == USER and start["tenant"] == TENANT and start["image_version"] == "e2e"
    assert {t["name"] for t in start["tools"]} >= {"bash", "question", "read"}
    assert g.of("clock")[0] == {"kind": "host_hello", "at": T0}
    # the request the model saw: system block without {current_time}, preamble + text
    req = g.llm.requests[0]
    assert req["model"] == "fake-model"
    assert req["stream"] is True and req["stream_options"] == {"include_usage": True}
    assert "{current_time}" not in req["messages"][0]["content"]
    assert req["messages"][1]["content"].startswith("[context]\ntime: ")
    assert req["messages"][1]["content"].endswith("\n\nWhat is 2 + 2?")
    assert any(t["function"]["name"] == "bash" for t in req["tools"])
    assert "stream" not in g.of("turn_start")[0]["params"]
    assert g.of("turn_start")[0]["workspace_tree_id"] == guest.ops.workspace_tree_id(g.workspace)
    # live messages reached the host: one delta per streamed fragment, no whole-text delta
    kinds = [m["type"] for m in g.host.live]
    assert "ready" in kinds and "delta" in kinds
    deltas = [m["body"] for m in g.host.live if m["type"] == "delta"]
    turn_id = g.of("turn_start")[0]["turn_id"]
    assert len(deltas) > 1
    assert all(d["session_id"] == SID and d["turn_id"] == turn_id and d["kind"] == "content" for d in deltas)
    assert "".join(d["text"] for d in deltas) == "2 + 2 = 4."
    assert g.host.errors == []
    assert g.host.acked_seq == len(g.events)
    state = check_replay(g.events)
    assert state_hash(state) == state_hash(check_replay(g.local_log()))


def test_text_answer_without_streaming(guest_factory):
    """params.stream false (LLM_STREAM=false on the host): one JSON call per
    turn; the provider sends the whole text as one delta before llm_response."""
    g = guest_factory(hello_body(params={"temperature": 0, "stream": False}), script=[text("2 + 2 = 4.")])
    g.send_task("What is 2 + 2?")
    settle = g.host.wait_for("settle", timeout=10)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "2 + 2 = 4."
    req = g.llm.requests[0]
    assert req["stream"] is False and "stream_options" not in req
    assert g.llm.streamed == []
    deltas = [m["body"] for m in g.host.live if m["type"] == "delta"]
    assert deltas == [{"session_id": SID, "turn_id": g.of("turn_start")[0]["turn_id"],
                       "kind": "text", "index": 0, "text": "2 + 2 = 4."}]
    assert g.of("turn_start")[0]["params"] == {"temperature": 0, "max_tokens": 128}
    check_replay(g.events)


def test_cut_stream_is_retried_and_deltas_are_not_duplicated(guest_factory):
    """The fake cuts the first stream after three chunks; the provider retries,
    re-streams from the start and de-dups what it already handed on: the host
    sees every fragment once."""
    answer = "this answer is long enough to be cut in the middle of it"
    g = guest_factory(hello_body(), script=[{"cut_after": 3, "content": answer}, text(answer)])
    g.send_task("say it")
    settle = g.host.wait_for("settle", timeout=20)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == answer
    assert len(g.llm.requests) == 2 and len(g.llm.streamed) == 2
    deltas = [m["body"] for m in g.host.live if m["type"] == "delta"]
    assert "".join(d["text"] for d in deltas) == answer
    assert len(deltas) == len(answer.split())          # one per word fragment, none twice
    assert g.of("llm_response")[0]["message"]["content"] == answer
    assert g.types() == ["session_start", "clock", "input", "clock", "turn_start", "llm_response", "settle"]
    check_replay(g.events)


def test_bash_echo_then_answer(guest_factory):
    g = guest_factory(hello_body(), script=[
        tool_calls([("call_echo", "bash", {"command": "echo hello from the guest"})]),
        text("It said hello."),
    ])
    g.send_task("run echo")
    settle = g.host.wait_for("settle", timeout=15)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "It said hello."
    assert g.types() == [
        "session_start", "clock", "input", "clock", "turn_start", "llm_response",
        "guard_decision", "tool_call", "op_state", "op_state", "tool_result", "wake",
        "clock", "turn_start", "llm_response", "settle"]
    call = g.of("tool_call")[0]
    assert call["name"] == "bash" and call["side"] == "guest" and call["call_id"] == "call_echo"
    # the streamed tool call reached the host as fragments; llm_response holds the assembled call
    tc_deltas = [m["body"] for m in g.host.live if m["type"] == "delta" and m["body"]["kind"] == "tool_call"]
    assert tc_deltas and json.loads("".join(d["text"] for d in tc_deltas)) == {"command": "echo hello from the guest"}
    assert g.of("llm_response")[0]["message"]["tool_calls"][0]["function"]["arguments"] == json.dumps(
        {"command": "echo hello from the guest"}, separators=(",", ":"))
    assert [s["state"] for s in g.of("op_state")] == ["running", "done"]
    assert g.of("tool_result")[0]["content"].strip() == "hello from the guest"
    assert g.of("wake")[0] == {"reason": "tool_result", "result_ids": ["call_echo"], "input_ids": []}
    # second request: prefix of the first plus the round, tool message verbatim
    r1, r2 = g.llm.requests
    assert r2["messages"][: len(r1["messages"])] == r1["messages"]
    assert r2["messages"][-1]["role"] == "tool" and r2["messages"][-1]["tool_call_id"] == "call_echo"
    check_replay(g.events)


def test_device_tool_answered_by_side_results(guest_factory):
    g = guest_factory(
        hello_body([{"name": "calendar", "side": "device", "pin": None}]),
        script=[tool_calls([("call_dev", "calendar", {"day": "today"})]),
                text("You have Standup at 09:30.")],
        side_results={"call_dev": {"content": '[{"title":"Standup","start":"09:30"}]', "ok": True}},
    )
    g.send_task("What is on my calendar today?")
    settle = g.host.wait_for("settle", timeout=15)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done"
    assert g.types() == [
        "session_start", "clock", "input", "clock", "turn_start", "llm_response",
        "guard_decision", "tool_call", "op_state", "op_state", "tool_result", "wake",
        "clock", "turn_start", "llm_response", "settle"]
    assert g.of("tool_call")[0]["side"] == "device"
    ops = g.of("op_state")
    assert ops[0] == {"call_id": "call_dev", "state": "running", "side_ref": "device"}
    assert ops[1]["state"] == "done" and ops[1]["duration_ms"] >= 0
    assert g.of("tool_result")[0] == {"call_id": "call_dev", "content": '[{"title":"Standup","start":"09:30"}]'}
    # the side_result control was acked by the guest
    g.host.wait_acked_cseq(g.host.outbox.highest_sent, timeout=5)
    check_replay(g.events)


def test_host_memory_fact_on_side_result_becomes_a_memory_event(guest_factory):
    sha = "ef" * 32
    g = guest_factory(
        hello_body([{"name": "remember", "side": "host", "pin": None}]),
        script=[tool_calls([("call_mem", "remember", {"scope": "user", "slug": "tea", "text": "oolong"})]),
                text("Noted.")],
        side_results={"call_mem": {"content": "saved", "ok": True,
                                   "memory": {"scope": "user", "op": "write", "slug": "tea", "sha256": sha}}},
    )
    g.send_task("remember that I like oolong")
    settle = g.host.wait_for("settle", timeout=15)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done"
    types = g.types()
    assert types[types.index("tool_result") + 1] == "memory"
    assert g.of("memory") == [{"scope": "user", "op": "write", "slug": "tea", "sha256": sha,
                               "derived_from": "call_mem"}]
    check_replay(g.events)


def test_redelivered_task_runs_once(guest_factory):
    gate = threading.Event()

    def gated(request):
        gate.wait(10)
        return text("done once")

    g = guest_factory(hello_body(), script=[gated])
    msg = g.send_task("do it", input_id="in-dup")
    g.host.wait_for("turn_start", timeout=10)
    g.host.redeliver(msg)                                   # the same control line again
    g.send_task("do it", input_id="in-dup")                 # a fresh control, same input_id
    g.host.wait_acked_cseq(g.host.outbox.highest_sent, timeout=5)
    gate.set()
    settle = g.host.wait_for("settle", timeout=10)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["input_id"] == "in-dup" and settle["answer"] == "done once"
    inputs = g.of("input")
    assert [i["dedupe"] for i in inputs] == ["new", "duplicate"]
    assert len(g.of("turn_start")) == 1 and len(g.llm.requests) == 1
    check_replay(g.events)


def test_drop_link_mid_turn_no_duplicate_events_and_log_replays(guest_factory):
    gate = threading.Event()

    def gated(request):
        gate.wait(10)
        return text("after the drop")

    g = guest_factory(hello_body(), script=[gated])
    g.send_task("hold on")
    g.host.wait_for("turn_start", timeout=10)
    n_before = len(g.events)
    g.host.drop_link()
    g.host.wait_connected(timeout=10, count=2)              # the guest reconnected
    deadline = time.time() + 5
    while not g.host.handshakes and time.time() < deadline:   # its first line is the handshake
        time.sleep(0.01)
    assert g.host.handshakes, "the guest's first line on reconnect is its handshake"
    gate.set()
    settle = g.host.wait_for("settle", timeout=10)["body"]
    assert g.finish() == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "after the drop"
    seqs = [e["seq"] for e in g.events]
    assert seqs == list(range(1, len(seqs) + 1))            # delivered once each, gapless
    assert len(g.events) > n_before
    assert g.host.errors == []
    state = check_replay(g.events)
    local = g.local_log()
    assert state_hash(check_replay(local)) == state_hash(state)
    # every event the guest buffered reached the host exactly once
    assert [e["seq"] for e in local] == seqs


# ---------------------------------------------------------------- compaction over 40 turns
ROUNDS = 40


def _brain(request: dict):
    """The scripted model: a short summary for a compaction request (no
    tools offered), a bash call for the first ROUNDS regular requests, then
    the answer. usage.prompt_tokens is sized like the request, as a real
    server's would be."""
    n = len(json.dumps(request, separators=(",", ":")))
    usage = {"prompt_tokens": -(-n // 4), "completion_tokens": 8, "total_tokens": -(-n // 4) + 8}
    if "tools" not in request:
        done = sum(1 for m in request["messages"] if m.get("role") == "tool")
        return text(f"SUMMARY: goal: count lines; progress: {done} echoes done since the last "
                    f"summary; next: keep going.", usage=usage)
    if _brain.calls < ROUNDS:
        _brain.calls += 1
        return tool_calls([(f"call_{_brain.calls}", "bash", {"command": f"echo line {_brain.calls}"})],
                          usage=usage)
    return text(f"Printed {ROUNDS} lines.", usage=usage)


def test_forty_turn_session_compacts_and_replays(guest_factory):
    _brain.calls = 0
    hello = hello_body(limits={
        "wall_clock_s": 120, "max_tools": ROUNDS + 5, "max_turns": ROUNDS + 10, "max_tokens": 128,
        "op_timeout_s": {"device": 5, "host": 5}, "grace_s": 0.2, "idle_wake_s": 600,
        # the base request (system + built-in tool schemas) is ~1300 tokens; every
        # echo round adds ~60, so the budget of 1800 compacts every ~8 rounds
        "context_window_tokens": 2000, "reserve_tokens": 200,
    })
    g = guest_factory(hello, script=[_brain] * (ROUNDS * 2 + 20))
    g.send_task("echo 40 lines, one per turn")
    settle = g.host.wait_for("settle", timeout=90)["body"]
    assert g.finish(timeout=30) == 0, g.stderr
    assert settle["outcome"] == "done" and settle["answer"] == f"Printed {ROUNDS} lines."
    assert g.host.errors == []
    events = g.events
    starts = g.of("turn_start")
    regular = [t for t in starts if t["type"] == "regular"]
    compactions = g.of("compaction")
    assert len(regular) == ROUNDS + 1
    assert len(compactions) >= 2
    assert len([t for t in starts if t["type"] == "compaction"]) == len(compactions)
    assert len(g.of("tool_result")) == ROUNDS
    assert all(t["workspace_tree_id"] for t in starts)
    # each compaction: the range covers the committed prefix of that moment, the
    # summary is in memory, the compaction request offered no tools
    memories = g.of("memory")
    assert [m["slug"] for m in memories] == [
        f"compaction-{t['seq']}" for t in g.events if t["type"] == "turn_start" and t["body"]["type"] == "compaction"]
    assert all(m["scope"] == "session" and m["op"] == "write" and len(m["sha256"]) == 64 for m in memories)
    assert {c["summary_turn_id"] for c in compactions} == {m["derived_from"] for m in memories}
    by_turn = {t["turn_id"]: t for t in starts}
    input_seq = next(e["seq"] for e in events if e["type"] == "input")
    for c in compactions:
        turn = by_turn[c["summary_turn_id"]]
        assert turn["type"] == "compaction" and c["range"][1] == turn["includes_through_seq"]
        assert c["range"][0] == input_seq
        assert c["tokens_before"] > 1800 and c["tokens_after"] < c["tokens_before"]
    compaction_requests = [r for r in g.llm.requests if "tools" not in r]
    assert len(compaction_requests) == len(compactions)
    assert all(r["messages"][-1]["content"].startswith("[compaction]") for r in compaction_requests)
    # the prefix stays bounded: at no turn start did the model see the whole history
    widest = 0
    for ev in events:
        if ev["type"] == "turn_start":
            widest = max(widest, len(fold(events, upto_seq=ev["seq"]).committed_prefix))
    assert widest < ROUNDS, f"prefix reached {widest} messages"
    print(f"compactions={len(compactions)} widest_prefix={widest} events={len(events)}")
    final = check_replay(events)
    assert final.compacted_through == compactions[-1]["range"][1]
    assert final.committed_prefix[0]["content"].startswith("SUMMARY")
    assert state_hash(final) == state_hash(check_replay(g.local_log()))
    assert g.host.acked_seq == len(events)
