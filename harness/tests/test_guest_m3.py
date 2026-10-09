"""M3 end to end: guest.main as a subprocess, FakeHost (:5000 + :5001 bulk)
and FakeLLM (:5010) — park, resume, death and log return, the settle
manifest, spill uploads and the vsock idle-wait (docs/M3-CONTRACTS.md).

Every scenario checks that the host's copy of the log validates, folds and
that its checkpoints verify (fold.replay), and that seq runs gapless across
the park or the restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from guest.coordinator import LOST_CONTENT
from protocol.canonical import canonical_json
from protocol.fold import replay, state_hash, verify_checkpoints
from tests.fakes.fake_host import HOLD, FakeHost
from tests.fakes.fake_llm import FakeLLM, text, tool_calls
from tests.test_guest_e2e import check_replay, hello_body

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICE = [{"name": "calendar", "side": "device", "pin": None}]


class Rig:
    """FakeHost + FakeLLM + guest processes (one at a time, possibly several
    over the test: a killed guest is replaced by a new process)."""

    def __init__(self, tmp_path, hello, script=(), side_results=None, **host_kw):
        self.tmp = tmp_path
        self.dir = str(tmp_path / "sock")
        self.workspace = str(tmp_path / "workspace")
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.workspace, exist_ok=True)
        self.hello = hello
        self.llm = FakeLLM(self.dir, list(script)).start()
        self.host = FakeHost(self.dir, hello, side_results, **host_kw).start()
        self.procs: list[subprocess.Popen] = []
        self.runs = 0

    def spawn(self, *extra, wait_hello=True) -> subprocess.Popen:
        self.runs += 1
        log_dir = str(self.tmp / f"log{self.runs}")
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [sys.executable, "-m", "guest.main", "--connect", f"unix:{self.dir}",
             "--log-dir", log_dir, "--workspace", self.workspace, "--hello-timeout", "10",
             "--image-version", "m3", *extra],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        proc.log_dir = log_dir
        self.procs.append(proc)
        if wait_hello:
            self.host.wait_connected(timeout=10, count=self.host.connections + 1)
            self.host.wait_acked_cseq(1, timeout=10)
        return proc

    def kill(self, proc) -> None:
        """SIGKILL the guest and everything it started (the VM dies)."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()

    def finish(self, proc, timeout=20.0) -> int:
        """End the session the way a host stop does (abort: the guest stays
        up after a settle otherwise) and collect the exit code."""
        if proc.poll() is None and self.host.connected:
            self.host.send("abort", {"reason": "test over"})
        try:
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill(proc)
            raise AssertionError("guest did not exit")
        self.stderr = err.decode("utf-8", "replace")
        return proc.returncode

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                self.kill(p)
        self.host.stop()
        self.llm.stop()

    def task(self, text_="go", input_id="in-1"):
        return self.host.send("task", {"input_id": input_id, "text": text_, "attachments": []})

    def types(self) -> list:
        return [e["type"] for e in self.host.events]

    def of(self, type) -> list:
        return [e["body"] for e in self.host.events if e["type"] == type]


def local_log(log_dir) -> list:
    events = []
    d = os.path.join(log_dir, "events")
    for name in sorted(os.listdir(d)):
        if name.endswith(".jsonl"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                events.extend(json.loads(line) for line in fh if line.strip())
    return events


def check_parked_log(events) -> None:
    """The log replays clean, every checkpoint verifies, and each parked
    names the checkpoint right before it."""
    state = check_replay(events)
    assert verify_checkpoints(events) == []
    for i, e in enumerate(events):
        if e["type"] == "parked":
            assert events[i - 1]["type"] == "checkpoint"
            assert e["body"]["checkpoint_seq"] == events[i - 1]["seq"]
    return state


def wait_until(pred, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(*a, **kw) -> Rig:
        r = Rig(tmp_path, *a, **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.stop()


def m3_hello(extra_tools=(), **limits):
    h = hello_body(extra_tools)
    h["config"]["limits"].update(limits)
    return h


# ---------------------------------------------------------------- park
def test_park_at_turn_boundary_then_resume_same_seq(rig):
    """park lands while the model is thinking: the response's bash runs, the
    round completes, and the guest parks instead of starting turn 2. After
    the "restore" (the host listens again) it reconnects with a handshake
    and turn 2 runs; seq never restarts."""
    gate = threading.Event()

    def gated(request):
        gate.wait(10)
        return tool_calls([("call_echo", "bash", {"command": "echo parked soon"})])

    r = rig(m3_hello(), script=[gated, text("Resumed and done.")])
    proc = r.spawn()
    r.task()
    r.host.wait_for("turn_start", timeout=10)
    r.host.send("park", {"reason": "idle"})
    r.host.wait_acked_cseq(r.host.outbox.highest_sent, timeout=5)
    gate.set()
    parked = r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)          # the guest closed its link after the ack
    time.sleep(0.3)                              # idle-wait: nothing happens while parked
    assert r.types()[-1] == "parked" and not r.host.connected
    assert len(r.of("turn_start")) == 1 and len(r.llm.requests) == 1
    n_conn = r.host.connections
    r.host.listen()                              # the restore
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc) == 0, r.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "Resumed and done."
    assert r.host.connections == n_conn + 1 and r.host.handshakes, "resume = reconnect + handshake"
    types = r.types()
    i = types.index("parked")
    assert types[i - 1] == "checkpoint"
    assert types[i - 4:i - 1] == ["op_state", "tool_result", "wake"]   # the round finished first
    assert types[i + 1:] == ["clock", "turn_start", "llm_response", "settle"]
    assert parked["body"] == {"checkpoint_seq": parked["seq"] - 1, "reason": "idle",
                              "wall_used_ms": parked["body"]["wall_used_ms"], "at": parked["body"]["at"]}
    assert 0 <= parked["body"]["wall_used_ms"] < 60_000
    cp = r.of("checkpoint")[0]
    assert cp["through_seq"] == parked["seq"] - 2
    assert r.host.errors == []
    state = check_parked_log(r.host.events)
    assert state_hash(state) == state_hash(check_replay(local_log(proc.log_dir)))


def test_park_mid_bash_waits_the_grace_then_kills(rig):
    r = rig(m3_hello(park_grace_s=0.5), script=[
        tool_calls([("call_sleep", "bash", {"command": "sleep 30"})]),
        text("The sleep was killed.")])
    proc = r.spawn()
    r.task()
    wait_until(lambda: [s["state"] for s in r.of("op_state")] == ["running"], what="bash running")
    t0 = time.monotonic()
    r.host.send("park", {"reason": "question"})
    r.host.wait_for("parked", timeout=10)
    assert time.monotonic() - t0 < 10
    types = r.types()
    i = types.index("parked")
    assert types[types.index("tool_call"):i + 1] == [
        "tool_call", "op_state", "clock", "clock", "op_state", "tool_result", "wake", "checkpoint",
        "parked"]
    # the round's grace (0.2 s, no effect in sync mode), then the park's
    assert [c["kind"] for c in r.of("clock")][-2:] == ["grace_expiry", "grace_park"]
    assert [s["state"] for s in r.of("op_state")] == ["running", "killed"]
    r.host.wait_disconnected(timeout=5)
    r.host.listen()
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc) == 0, r.stderr
    assert settle["outcome"] == "done"
    # the model saw the killed result
    assert r.llm.requests[1]["messages"][-1]["role"] == "tool"
    check_parked_log(r.host.events)


def test_park_while_a_host_op_runs_and_its_result_after_resume(rig):
    r = rig(m3_hello(DEVICE), script=[
        tool_calls([("call_dev", "calendar", {"day": "today"})]),
        text("Standup at 09:30.")], side_results={"call_dev": HOLD})
    proc = r.spawn()
    r.task()
    r.host.wait_for("op_state", timeout=10)                # running, side device
    r.host.send("park", {"reason": "idle"})
    r.host.wait_for("parked", timeout=10)                   # at once: only a host op runs
    r.host.wait_disconnected(timeout=5)
    # the side result reaches the worker while the VM is parked: held until resume
    r.host.send("side_result", {"call_id": "call_dev", "side": "device",
                                "content": "Standup 09:30", "ok": True})
    r.host.listen()
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc) == 0, r.stderr
    assert settle["answer"] == "Standup at 09:30."
    types = r.types()
    assert types.index("parked") < types.index("tool_result")
    assert [s["state"] for s in r.of("op_state")] == ["running", "done"]
    assert r.of("tool_result")[0]["content"] == "Standup 09:30"
    check_parked_log(r.host.events)


def test_host_stops_a_parked_guest_then_resume_hello_keeps_the_host_op(rig):
    """The host stops a parked guest (its snapshot is lost); the next process
    gets hello{resume}, the log ends with parked, so the device call is still
    waited for (not lost)."""
    r = rig(m3_hello(DEVICE), script=[
        tool_calls([("call_dev", "calendar", {"day": "today"})]),
        text("All good.")], side_results={"call_dev": HOLD})
    proc = r.spawn()
    r.task()
    r.host.wait_for("op_state", timeout=10)
    r.host.send("park", {"reason": "idle"})
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)          # parked was acked; the guest idle-waits
    r.kill(proc)                                  # the host stops the parked VM
    r.host.listen()
    proc2 = r.spawn(wait_hello=False)
    r.host.wait_connected(timeout=10, count=r.host.connections + 1)
    hello = r.host.send("hello", dict(r.hello, resume=True))
    r.host.wait_acked_cseq(hello["cseq"], timeout=10)
    r.host.send("side_result", {"call_id": "call_dev", "side": "device", "content": "fine", "ok": True})
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc2) == 0, r.stderr
    assert settle["answer"] == "All good."
    assert [s["state"] for s in r.of("op_state")] == ["running", "done"]
    assert len(r.of("session_start")) == 1 and r.host.bulk_gets
    check_parked_log(r.host.events)
    assert [e["seq"] for e in local_log(proc2.log_dir)] == [e["seq"] for e in r.host.events]


# ---------------------------------------------------------------- death
def test_kill_mid_bash_resume_with_log_return(rig):
    r = rig(m3_hello(), script=[
        tool_calls([("call_sleep", "bash", {"command": "sleep 30"})]),
        text("Recovered after the restart.")])
    proc = r.spawn()
    r.task()
    wait_until(lambda: [s["state"] for s in r.of("op_state")] == ["running"], what="bash running")
    r.host.wait_for_seq(len(r.host.events))
    before = list(r.host.events)
    r.kill(proc)                                             # the VM dies mid-bash
    r.host.wait_disconnected(timeout=5)
    proc2 = r.spawn(wait_hello=False)                        # restored from the base snapshot
    r.host.wait_connected(timeout=10, count=r.host.connections + 1)
    hello = r.host.send("hello", dict(r.hello, resume=True))
    assert hello["cseq"] > 1
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc2) == 0, r.stderr
    assert settle["outcome"] == "done" and settle["answer"] == "Recovered after the restart."
    events = r.host.events
    assert events[:len(before)] == before
    after = [e["type"] for e in events[len(before):]]
    assert after == ["clock", "op_state", "tool_result", "wake", "clock", "turn_start",
                     "llm_response", "settle"]
    assert events[len(before)]["body"]["kind"] == "host_hello"
    assert r.of("op_state")[-1] == {"call_id": "call_sleep", "state": "lost"}
    assert r.of("tool_result")[0]["content"] == LOST_CONTENT
    assert r.of("wake")[-1] == {"reason": "tool_result", "result_ids": ["call_sleep"], "input_ids": []}
    assert len(r.of("session_start")) == 1
    assert r.host.bulk_gets == [{"op": "get", "session_id": r.hello["session_id"], "kind": "log",
                                 "after_seq": 0}]
    assert r.host.errors == []
    state = check_replay(events)
    # the new process's buffer = the returned log + what it wrote since
    assert state_hash(state) == state_hash(check_replay(local_log(proc2.log_dir)))
    # the model's next request carried the lost result
    assert r.llm.requests[-1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_sleep",
                                                   "content": LOST_CONTENT}


# ---------------------------------------------------------------- bulk
def test_settle_manifest_and_spill_upload(rig):
    big = "x" * 3000
    r = rig(m3_hello(), script=[
        tool_calls([("call_w", "bash", {"command": f"mkdir -p sub && echo hi > a.txt && "
                                                   f"printf '%s' {big} > sub/b.txt && printf '%s' {big}"})]),
        text("Wrote two files.")])
    proc = r.spawn()
    r.task()
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc) == 0, r.stderr
    manifest = settle["manifest"]
    assert manifest["version"] == 1
    paths = [f["path"] for f in manifest["files"]]
    assert paths == ["a.txt", "sub/b.txt"]                  # .harness/ (local spill) left out
    a = manifest["files"][0]
    assert a == {"path": "a.txt", "bytes": 3, "sha256": hashlib.sha256(b"hi\n").hexdigest(),
                 "mode": a["mode"]}
    assert manifest["total_bytes"] == 3 + 3000
    assert r.host.bulk_files[("workspace", "a.txt")] == b"hi\n"
    assert r.host.bulk_files[("workspace", "sub/b.txt")] == big.encode()
    assert r.host.bulk_files[("manifest", "manifest.json")] == canonical_json(manifest).encode()
    # the spill went out as kind spill under .spill/, and the events name that path
    spill = r.host.bulk_files[("spill", ".spill/call_w.txt")]
    assert spill.decode().endswith(big)
    bulk = r.of("bulk")[0]
    assert bulk == {"kind": "spill", "path": ".spill/call_w.txt",
                    "sha256": hashlib.sha256(spill).hexdigest(), "bytes": len(spill)}
    assert r.of("tool_result")[0]["spill_ref"] == ".spill/call_w.txt"
    kinds = [h["kind"] for h in r.host.bulk_puts]
    assert kinds == ["spill", "workspace", "workspace", "manifest"]
    check_replay(r.host.events)


# ---------------------------------------------------------------- idle-wait
def test_idle_wait_vsock_connects_when_the_host_appears(rig, tmp_path):
    r = rig(m3_hello(), script=[text("Hi.")])
    r.host.close_listener()                                  # the snapshot point: nobody listens
    proc = r.spawn("--idle-wait", "vsock", "--mount-workspace", str(tmp_path / "no-such-dev"),
                   wait_hello=False)
    time.sleep(0.5)
    assert proc.poll() is None and r.host.connections == 0   # silent polling, no exit
    r.host.listen()                                          # restore: the listener exists
    r.host.wait_connected(timeout=5)
    r.host.wait_acked_cseq(1, timeout=10)
    r.task()
    settle = r.host.wait_for("settle", timeout=10)["body"]
    assert r.finish(proc) == 0, r.stderr
    assert settle["answer"] == "Hi."
    assert "absent; not mounting" in r.stderr
    check_replay(r.host.events)


def test_local_sandbox_style_sigstop_park_and_sigcont_resume(rig):
    """LocalSandbox: snapshot = SIGSTOP the parked guest, restore(session) =
    SIGCONT. The same process resumes with its memory: a reconnect with a
    handshake, then the task the worker held while it was stopped."""
    r = rig(m3_hello(), script=[tool_calls([("c1", "bash", {"command": "echo 1"})]), text("Done.")])
    proc = r.spawn("--idle-wait", "vsock")
    r.host.send("park", {"reason": "idle"})              # no task yet: parks at once
    r.host.wait_for("parked", timeout=10)
    r.host.wait_disconnected(timeout=5)
    os.kill(proc.pid, signal.SIGSTOP)
    r.task()                                              # pending at the worker while stopped
    time.sleep(0.2)
    r.host.listen()
    os.kill(proc.pid, signal.SIGCONT)
    settle = r.host.wait_for("settle", timeout=15)["body"]
    assert r.finish(proc) == 0, r.stderr
    assert settle["answer"] == "Done."
    assert r.types()[:4] == ["session_start", "clock", "checkpoint", "parked"]
    assert r.host.handshakes
    check_parked_log(r.host.events)
