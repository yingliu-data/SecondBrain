"""M3 units: the coordinator's park, restore and determinism (fakes from
test_guest_coordinator), the transport's hello floor / seed / park, the bulk
client against FakeHost's :5001, the workspace manifest, mount_workspace and
the Outbox/Inbox resume_from."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from guest import bulk as gbulk
from guest.coordinator import LOST_CONTENT, Coordinator
from guest.main import mount_workspace, rebuild_eventlog
from guest.transport import Transport, UnixConnector
from protocol import bulk as pbulk
from protocol.canonical import hash_value
from protocol.events import validate_log
from protocol.fold import State, replay, state_hash, verify_checkpoints
from protocol.messages import Inbox, Outbox, ProtocolError, control_message
from tests.fakes.fake_host import FakeHost
from tests.test_guest_coordinator import (
    DEVICE, FakeOps, FakeTransport, Scenario, T0, make_cfg, text, tool_calls,
)

ROOT = Path(__file__).resolve().parents[1]
SID = "sess-coord"


def full_check(events):
    assert validate_log(events) == []
    state, errors = replay(events)
    assert errors == []
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    return state


# ---------------------------------------------------------------- fakes
class ParkTransport(FakeTransport):
    """FakeTransport plus what a park needs: wait_acked (acks everything at
    once) and park() (recorded)."""

    def __init__(self):
        super().__init__()
        self.parks = 0

    def wait_acked(self, seq, timeout=None):
        return True

    @property
    def acked_seq(self):
        return self.events[-1]["seq"] if self.events else 0

    def park(self):
        self.parks += 1


class KillingOps(FakeOps):
    """abort_all reports every running op as killed, like the real manager."""

    def abort_all(self):
        self.aborted += 1
        for cid in list(self._running):
            self.complete(cid, {"ok": False, "content": "[killed: aborted]", "state": "killed",
                                "duration_ms": 1, "spill": None})


def park_scenario(cfg=None, script=(), ops_manual=False):
    scn = Scenario(cfg=cfg, script=script, ops_manual=ops_manual)
    scn.transport = ParkTransport()
    scn.log.transport = scn.transport
    scn.ops = KillingOps(scn.inbound, None, ops_manual)
    scn.coord.transport = scn.transport
    scn.coord.ops = scn.ops
    return scn


# ---------------------------------------------------------------- coordinator: park
def test_park_waits_for_the_provider_then_parks_at_the_turn_boundary():
    scn = park_scenario(script=[tool_calls([("c1", "bash", {"command": "true"})]), text("after")],
                        ops_manual=False)
    scn.provider.hold = True
    scn.task("x")
    scn.start()
    scn.wait_event("turn_start")
    scn.control("park", {"reason": "idle"})
    time.sleep(0.05)
    assert "parked" not in scn.types()          # a provider call is in flight
    scn.provider.release()
    scn.wait_event("parked")
    types = scn.types()
    assert types[-5:] == ["op_state", "tool_result", "wake", "checkpoint", "parked"]
    assert scn.transport.parks == 1 and scn.coord.parked
    cp = scn.of("checkpoint")[0]
    assert cp["body"]["prev_hash"] == hash_value(scn.events[0])          # chain head
    assert cp["body"]["through_seq"] == cp["seq"] - 1
    assert scn.of("parked")[0]["body"] == {"checkpoint_seq": cp["seq"], "reason": "idle"}
    assert len(scn.provider.requests) == 1                               # turn 2 deferred
    scn.inbound.put(("link", "up"))
    scn.run()
    assert scn.types()[-3:] == ["turn_start", "llm_response", "settle"]
    assert verify_checkpoints(scn.events) == []
    full_check(scn.events)


def test_park_grace_kills_guest_ops_and_records_killed():
    scn = park_scenario(script=[tool_calls([("c1", "bash", {"command": "sleep 99"})]), text("ok")],
                        ops_manual=True)
    scn.task("x")
    scn.start()
    scn.wait_for(lambda: scn.ops.submitted)
    scn.control("park", {"reason": "question"})
    scn.wait_for(lambda: scn.clock.pending(30.0))     # park_grace_s default 30
    assert "parked" not in scn.types()
    scn.clock.fire(30.0)
    scn.wait_event("parked")
    ops = [e["body"] for e in scn.of("op_state")]
    assert [o["state"] for o in ops] == ["running", "killed"]
    assert any(e["body"] == {"kind": "grace_park", "at": e["body"]["at"]} for e in scn.of("clock"))
    assert scn.of("tool_result")[0]["body"]["content"] == "[killed: aborted]"
    scn.inbound.put(("link", "up"))
    scn.run()
    full_check(scn.events)


def test_host_op_does_not_hold_a_park_and_timers_pause():
    scn = park_scenario(cfg=make_cfg(DEVICE), script=[
        tool_calls([("c_dev", "calendar", {})]), text("done")])
    scn.task("x")
    scn.start()
    scn.wait_for(lambda: any(e["body"].get("side_ref") for e in scn.of("op_state")))
    scn.clock.advance(10_000)
    scn.control("park", {"reason": "idle"})
    scn.wait_event("parked")
    assert scn.clock.pending(300.0) == [] and scn.clock.pending(60.0) == []   # wall + device paused
    scn.clock.advance(3_600_000)                     # an hour parked
    scn.inbound.put(("link", "up"))
    scn.wait_for(lambda: len(scn.clock.pending()) >= 2)
    delays = sorted(h.delay_s for h in scn.clock.pending())
    assert any(280 < d < 300 for d in delays)        # wall clock: what it had left, not 0
    assert any(45 < d < 60 for d in delays)          # device op timeout: likewise
    scn.control("side_result", {"call_id": "c_dev", "side": "device", "content": "09:30", "ok": True})
    result = scn.run()
    assert result["outcome"] == "done"
    assert scn.types().index("parked") < scn.types().index("tool_result")
    full_check(scn.events)


def test_second_park_while_parking_is_ignored_and_run_handles_a_second_task():
    scn = park_scenario(script=[text("one"), text("two")])
    scn.task("x")
    first = scn.run()
    assert first["answer"] == "one"
    scn.thread = None
    scn.control("park", {"reason": "idle"})
    scn.control("park", {"reason": "again"})
    scn.task("y", input_id="in-2")
    scn.start()
    scn.wait_event("input", n=2)
    assert len(scn.of("parked")) == 1
    scn.inbound.put(("link", "up"))
    second = scn.run()
    assert second["answer"] == "two" and second["input_id"] == "in-2"
    full_check(scn.events)


# ---------------------------------------------------------------- coordinator: restore
def _run_until(scn, type_, n=1):
    scn.start()
    scn.wait_event(type_, n)
    return list(scn.events)


def test_restore_marks_open_calls_lost_and_continues():
    a = Scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c_dev", "calendar", {"d": 1})])])
    a.task("what's on")
    log = _run_until(a, "op_state")                  # the device call runs; the VM dies here
    b = Scenario(cfg=make_cfg(DEVICE, resume=True, at=T0 + 9_000), script=[text("recovered")])
    b.coord.restore(log)
    assert b.coord.task_input_id == "in-1" and b.coord._side_calls
    result = b.run()
    assert result["outcome"] == "done" and result["answer"] == "recovered"
    new = b.events
    assert [e["type"] for e in new] == ["clock", "op_state", "tool_result", "wake", "clock",
                                        "turn_start", "llm_response", "settle"]
    assert new[0]["body"] == {"kind": "host_hello", "at": T0 + 9_000}
    assert new[1]["body"] == {"call_id": "c_dev", "state": "lost"}
    assert new[2]["body"]["content"] == LOST_CONTENT
    assert new[0]["seq"] == log[-1]["seq"] + 1
    state = full_check(log + new)
    assert state_hash(state) == state_hash(b.state)
    # the second request carries the first turn's prefix verbatim (preamble rebuilt)
    r1 = a.provider.requests[0]
    r2 = b.provider.requests[0]
    assert r2.messages[:len(r1.messages)] == r1.messages


def test_restore_of_a_lost_turn_records_an_error_and_reruns_it():
    a = Scenario(script=[text("never seen")], hold_at=(1,))
    a.task("hi")
    log = _run_until(a, "turn_start")
    b = Scenario(cfg=make_cfg(resume=True), script=[text("second try")])
    b.coord.restore(log)
    result = b.run()
    assert result["answer"] == "second try"
    assert [e["type"] for e in b.events] == ["clock", "error", "clock", "turn_start",
                                             "llm_response", "settle"]
    assert b.events[1]["body"]["class"] == "transient" and b.events[1]["body"]["retry"] is True
    full_check(log + b.events)
    a.provider.release()


def test_restore_when_the_answer_was_recorded_settles_it():
    a = Scenario(script=[text("the answer")])
    a.task("q")
    log = [e for e in _run_until(a, "settle") if e["type"] != "settle"]
    b = Scenario(cfg=make_cfg(resume=True))
    b.coord.restore(log)
    result = b.run()
    assert result["answer"] == "the answer" and b.provider.requests == []
    full_check(log + b.events)


def test_restore_after_parked_keeps_waiting_for_the_host():
    a = park_scenario(cfg=make_cfg(DEVICE), script=[tool_calls([("c_dev", "calendar", {})])])
    a.task("x")
    a.start()
    a.wait_for(lambda: any(e["body"].get("side_ref") for e in a.of("op_state")))
    a.control("park", {"reason": "idle"})
    a.wait_event("parked")
    log = list(a.events)
    b = Scenario(cfg=make_cfg(DEVICE, resume=True), script=[text("fine")])
    b.coord.restore(log)
    b.start()
    b.wait_for(lambda: b.events)                       # clock{host_hello} only
    assert [e["type"] for e in b.events] == ["clock"]
    b.control("side_result", {"call_id": "c_dev", "side": "device", "content": "ok", "ok": True})
    assert b.run()["answer"] == "fine"
    assert "lost" not in [e["body"]["state"] for e in b.events if e["type"] == "op_state"]
    events = log + b.events
    full_check(events)
    assert verify_checkpoints(events) == []


def test_restore_rejects_a_foreign_log():
    a = Scenario(script=[text("x")])
    a.task("q")
    log = _run_until(a, "settle")
    b = Scenario(cfg=make_cfg(session_id="sess-other"))
    with pytest.raises(ValueError):
        b.coord.restore(log)


# ---------------------------------------------------------------- determinism
def _session_log(session_id):
    scn = Scenario(cfg=make_cfg(session_id=session_id), script=[
        tool_calls([("call_1", "bash", {"command": "echo"})]),
        tool_calls([("call_2", "question", {"text": "ok?", "options": ["yes", "no"]})]),
        text("fin")])
    scn.answer_questions_with("yes")
    scn.task("go")
    scn.run()
    return scn.events


def _bodies(events):
    """Event bodies minus the fake LLM's response ids (a counter in the fake)."""
    return [{k: v for k, v in e["body"].items() if k != "response_id"} for e in events]


def _ids(events):
    return [(e["body"].get("turn_id") or e["body"].get("question_id"))
            for e in events if e["type"] in ("turn_start", "question")]


def test_same_pre_hello_state_different_session_ids_give_distinct_ids_and_logs():
    a1, a2, b = _session_log("sess-a"), _session_log("sess-a"), _session_log("sess-b")
    # the same session replays to the same ids and bodies: nothing random
    assert _ids(a1) == _ids(a2)
    assert _bodies(a1) == _bodies(a2)
    # another session (another VM restored from the same snapshot) differs
    assert set(_ids(a1)).isdisjoint(_ids(b))
    assert _bodies(a1) != _bodies(b)
    assert state_hash(full_check(a1)) != state_hash(full_check(b))


def test_no_randomness_or_uuid_in_the_control_path():
    """The guest and protocol packages import no RNG or uuid source (hooks.py
    imports random only to fence it off from hooks)."""
    bad = []
    for pkg in ("guest", "protocol"):
        for py in (ROOT / pkg).rglob("*.py"):
            if py.name == "hooks.py":
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Attribute) and node.attr == "urandom":
                    names = ["os.urandom"]
                for n in names:
                    if n.split(".")[0] in ("random", "uuid", "secrets") or n == "os.urandom":
                        bad.append(f"{py.relative_to(ROOT)}: {n}")
    assert bad == []


# ---------------------------------------------------------------- transport
HELLO = {"session_id": "sess-t", "principal": {"user": "u", "tenant": "t"},
         "config": {"model": "m"}, "system_block": {}, "resume": True, "at": 1}


def _wait(inbound, kind, type_=None, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            item = inbound.get(timeout=0.1)
        except queue.Empty:
            continue
        if item[0] == kind and (type_ is None or item[1].get("type") == type_):
            return item
    raise AssertionError(f"no {kind} {type_}")


def test_fresh_transport_takes_the_resume_hellos_cseq_as_its_floor(tmp_path):
    with FakeHost(tmp_path, HELLO, auto_hello=False) as host:
        for _ in range(4):                               # cseq 1..4 went to the dead guest
            host.send("task", {"input_id": "old", "text": "x"}, deliver=False)
        inbound = queue.Queue()
        t = Transport(UnixConnector(tmp_path), inbound, backoff_initial=0.02)
        t.start()
        try:
            host.wait_connected()
            host.redeliver(host.outbox.pending[2])       # a stale control before the hello: a gap
            hello = host.send("hello", HELLO)
            assert hello["cseq"] == 5
            item = _wait(inbound, "control")
            assert item[1]["type"] == "hello"            # the stale one was never delivered
            host.wait_acked_cseq(5)
            assert t.last_delivered_cseq == 5
            t.seed_seq(41)                               # the log return said 1..41
            t.send_event({"session_id": "sess-t", "seq": 42, "ts": "x", "schema": 1,
                          "type": "clock", "body": {"kind": "host_hello", "at": 1}})
            deadline = time.time() + 5                  # numbered after the returned log
            while not host.raw_events and time.time() < deadline:
                time.sleep(0.01)
            assert [e["seq"] for e in host.raw_events] == [42]
        finally:
            t.stop()


def test_outbox_and_inbox_resume_from():
    o = Outbox("s", "seq")
    o.resume_from(7)
    assert o.next_number() == 8 and o.highest_acked == 7
    with pytest.raises(ProtocolError):
        o.resume_from(9)
    i = Inbox("s", "cseq")
    i.resume_from(4)
    assert i.accept(control_message("s", 5, "hello", {})) == "new"
    with pytest.raises(ProtocolError):
        i.resume_from(1)


def test_transport_park_goes_idle_and_reconnects_with_a_handshake(tmp_path):
    hello = dict(HELLO, resume=False)
    with FakeHost(tmp_path, hello) as host:
        inbound = queue.Queue()
        t = Transport(UnixConnector(tmp_path), inbound, idle=True, idle_poll_s=0.02)
        t.start()
        try:
            assert t.wait_up(5)
            _wait(inbound, "control", "hello")
            host.close_listener()
            t.park()
            host.wait_disconnected()
            assert t.idle
            time.sleep(0.2)
            assert not t.connected and host.connections == 1
            host.listen()
            assert t.wait_up(5)
            assert host.handshakes == [1] and not t.idle
        finally:
            t.stop()


def test_idle_transport_polls_silently_until_a_listener_exists(tmp_path):
    inbound = queue.Queue()
    t = Transport(UnixConnector(tmp_path), inbound, idle=True, idle_poll_s=0.01)
    t.start()
    try:
        assert not t.wait_up(0.2)
        with FakeHost(tmp_path, dict(HELLO, resume=False)):
            assert t.wait_up(5)
    finally:
        t.stop()


def test_idle_transport_survives_a_reset_right_after_connect(tmp_path):
    """A snapshot restore resets vsock connections: a connection that dies
    before the host said anything sends the guest back to silent polling."""
    path = os.path.join(str(tmp_path), "v.sock_5000")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    accepted = []

    def reset_all():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            accepted.append(time.monotonic())
            c.close()
    threading.Thread(target=reset_all, daemon=True).start()
    inbound = queue.Queue()
    t = Transport(UnixConnector(tmp_path), inbound, idle=True, idle_poll_s=0.02,
                  backoff_initial=1.0)
    t.start()
    try:
        time.sleep(0.4)
        assert t.idle and len(accepted) >= 5          # ~every 20 ms, no 1 s backoff
        srv.close()
        os.unlink(path)
        with FakeHost(tmp_path, dict(HELLO, resume=False)):
            assert t.wait_up(5)
            _wait(inbound, "control", "hello")
            assert not t.idle
    finally:
        t.stop()


# ---------------------------------------------------------------- bulk client
def test_bulk_put_and_get_log_against_the_fake_host(tmp_path):
    with FakeHost(tmp_path, dict(HELLO, resume=False)) as host:
        c = gbulk.BulkClient(UnixConnector(tmp_path), "sess-t")
        c.put("spill", ".spill/c1.txt", b"abc")
        assert host.bulk_files[("spill", ".spill/c1.txt")] == b"abc"
        assert host.bulk_puts[0]["sha256"] == hashlib.sha256(b"abc").hexdigest()
        evs = [{"session_id": "sess-t", "seq": n, "ts": "x", "schema": 1, "type": "clock",
                "body": {"kind": "host_hello", "at": n}} for n in (1, 2, 3)]
        host.log_override = evs
        assert c.get_log(0) == (evs, 3)
        assert c.get_log(2) == (evs[2:], 3)
        assert c.get_log(3) == ([], 3)
        host.log_override = [evs[0], evs[2]]                    # a gap: refused
        with pytest.raises(pbulk.BulkError, match="gapless"):
            c.get_log(0)
        with pytest.raises(pbulk.BulkError):
            c.put("spill", "../escape", b"x")                   # refused before sending
        bad = gbulk.BulkClient(UnixConnector(tmp_path), "sess-other")
        with pytest.raises(pbulk.BulkError, match="foreign session"):
            bad.put("spill", ".spill/x", b"x")
        host.errors.clear()


def test_bulk_connect_failure_is_a_bulk_error(tmp_path):
    c = gbulk.BulkClient(UnixConnector(tmp_path), "s")
    with pytest.raises(pbulk.BulkError, match="connect"):
        c.put("spill", ".spill/x", b"x")


def test_workspace_manifest_excludes_spill_git_and_harness(tmp_path):
    ws = tmp_path / "ws"
    for rel, data in {"a.txt": b"a", "d/b.txt": b"bb", ".git/HEAD": b"ref",
                      ".spill/x.txt": b"s", ".harness/spill/y.txt": b"h", "d/.git/keep": b"k"}.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_bytes(data)
    os.symlink("a.txt", ws / "link")
    m = gbulk.settle_manifest(ws)
    assert [f["path"] for f in m["files"]] == ["a.txt", "d/.git/keep", "d/b.txt"]
    assert m["total_bytes"] == 4 and m["version"] == 1


def test_settle_manifest_uploads_only_changed_files(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes(b"one")
    (ws / "b.txt").write_bytes(b"two")
    with FakeHost(tmp_path, dict(HELLO, resume=False)) as host:
        c = gbulk.BulkClient(UnixConnector(tmp_path), "sess-t")
        uploaded = {}
        gbulk.settle_manifest(ws, c, uploaded)
        (ws / "b.txt").write_bytes(b"TWO")
        m = gbulk.settle_manifest(ws, c, uploaded)
        kinds = [(h["kind"], h["path"]) for h in host.bulk_puts]
        assert kinds == [("workspace", "a.txt"), ("workspace", "b.txt"), ("manifest", "manifest.json"),
                         ("workspace", "b.txt"), ("manifest", "manifest.json")]
        assert json.loads(host.bulk_files[("manifest", "manifest.json")]) == m


# ---------------------------------------------------------------- main helpers
def test_mount_workspace_paths(tmp_path):
    calls = []

    def ok(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"")

    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 32, b"mount: wrong fs type")

    dev = tmp_path / "vdb"
    assert mount_workspace(str(dev), str(tmp_path / "ws"), runner=ok) == "absent"
    dev.write_bytes(b"")
    assert mount_workspace(str(dev), str(tmp_path / "ws"), runner=ok) == "mounted"
    assert calls == [["mount", "-o", "noatime", str(dev), str(tmp_path / "ws")]]
    assert mount_workspace(str(dev), str(tmp_path / "ws"), runner=fail) == "failed"
    assert mount_workspace(str(dev), "/", runner=ok) == "already"


def test_rebuild_eventlog_moves_the_old_buffer_aside(tmp_path):
    from guest.eventlog import EventLog
    old = EventLog(tmp_path)
    old.append({"session_id": "s", "seq": 1, "ts": "x", "schema": 1, "type": "clock",
                "body": {"kind": "host_hello", "at": 1}})
    old.close()
    evs = [{"session_id": "s", "seq": n, "ts": "y", "schema": 1, "type": "clock",
            "body": {"kind": "host_hello", "at": n}} for n in (1, 2)]
    log = rebuild_eventlog(str(tmp_path), evs)
    assert log.last_seq == 2 and log.acked_seq == 2 and log.read() == evs
    assert (tmp_path / "events.before-resume-1").is_dir()
