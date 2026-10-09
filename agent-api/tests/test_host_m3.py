"""M3 host half (harness/docs/M3-CONTRACTS.md): the :5001 bulk server, park
and resume, the idle timer, base-snapshot boot, death resume with the log
return, vm host events. The guest side is the contract-speaking FakeGuest
(host_fakes.py) over the real Unix-socket endpoints."""
import asyncio
import json
import os
import socket
import time
from pathlib import Path

import pytest

from protocol.bulk import (decode_line, encode_line, get_header, put_header, read_exact,
                           read_line, sha256_hex)

from app.host.bulk_server import BulkServer
from app.host.endpoints import Endpoints
from app.host.sides import DeviceCalls
from app.host.state_sync import StateSync
from app.host.stream import DONE
from app.host.worker import Worker
from host_fakes import (HASH, LOST_TEXT, FakeRegistry, FakeSandbox, FakeTenant,  # noqa: F401
                        session, ts_now, users_root, worker_kwargs)

CALL = {"call": {"id": "call_d", "name": "get_calendar_events", "args": {}}}


async def drain(q: asyncio.Queue) -> list[str]:
    out = []
    while True:
        item = await asyncio.wait_for(q.get(), 10)
        if item is None:
            return out
        out.append(item)


async def until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def host(w, type_=None):
    return [h["body"] for h in w.sync.read_host() if type_ is None or h["type"] == type_]


def vm_ops(w):
    return [(b["op"], b.get("source")) for b in host(w, "vm")]


def assert_one_contiguous_run(w, guest_events=None):
    assert w.sync.runs() == [1]
    seqs = [e["seq"] for e in w.sync.read(0)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert sum(1 for e in w.sync.read(0) if e["type"] == "session_start") == 1


# ---------------------------------------------------------------- bulk server
def _ev(seq, type_, body):
    return {"session_id": "s1", "seq": seq, "ts": ts_now(), "schema": 1, "type": type_, "body": body}


@pytest.fixture
def bulk(session):
    ep = Endpoints(session.root, {"bulk": 5001})
    sync = StateSync(session)
    srv = BulkServer(ep.listener(5001), session.root, "s1", sync.read,
                     on_put=lambda *a: puts.append(a)).start()
    puts: list = []
    srv.puts = puts
    srv.sync = sync
    srv.path = ep.path(5001)
    yield srv
    srv.stop()
    ep.close()


def _transfer(path: Path, header: dict, payload: bytes = b"") -> tuple[dict, bytes]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    try:
        s.sendall(encode_line(header) + payload)
        f = s.makefile("rb")
        reply = decode_line(read_line(f))
        body = read_exact(f, reply["bytes"]) if reply.get("ok") and "bytes" in reply else b""
        return reply, body
    finally:
        s.close()


def test_bulk_put_spill_workspace_and_manifest(bulk, session):
    ws = session.root / "workspace"
    reply, _ = _transfer(bulk.path, put_header("s1", "spill", ".spill/r1.txt", b"x" * 5000),
                         b"x" * 5000)
    assert reply == {"ok": True}
    assert (ws / ".spill" / "r1.txt").read_bytes() == b"x" * 5000
    data = b"print('hi')\n"
    reply, _ = _transfer(bulk.path, put_header("s1", "workspace", "src/a.py", data), data)
    assert reply == {"ok": True} and (ws / "src" / "a.py").read_bytes() == data
    man = json.dumps({"version": 1, "files": [{"path": "src/a.py", "bytes": len(data),
                                               "sha256": sha256_hex(data), "mode": 420}],
                      "total_bytes": len(data)}).encode()
    reply, _ = _transfer(bulk.path, put_header("s1", "manifest", "manifest.json", man), man)
    assert reply == {"ok": True}
    assert json.loads((session.root / "manifest.json").read_text())["files"][0]["path"] == "src/a.py"
    assert not (ws / "manifest.json").exists()
    assert [p[0] for p in bulk.puts] == ["spill", "workspace", "manifest"]
    assert not list(ws.rglob(".bulk-*"))                    # no temp file left


def test_bulk_put_rejects_sha_mismatch_escape_and_foreign_session(bulk, session, tmp_path):
    ws = session.root / "workspace"
    h = put_header("s1", "workspace", "a.txt", b"good")
    reply, _ = _transfer(bulk.path, h, b"evil")             # same length, other bytes
    assert reply["ok"] is False and "sha256" in reply["error"]
    assert not (ws / "a.txt").exists() and not list(ws.rglob(".bulk-*"))
    # ".." and absolute paths: refused by the header check, nothing read
    for bad in ("../x.txt", "/etc/passwd", "a/../../x"):
        h = {"op": "put", "session_id": "s1", "kind": "workspace", "path": bad,
             "bytes": 1, "sha256": sha256_hex(b"x")}
        reply, _ = _transfer(bulk.path, h, b"x")
        assert reply["ok"] is False, bad
    # a symlink inside the workspace pointing out
    outside = tmp_path / "outside"
    outside.mkdir()
    ws.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, ws / "link")
    reply, _ = _transfer(bulk.path, put_header("s1", "workspace", "link/evil", b"x"), b"x")
    assert reply["ok"] is False and "escapes" in reply["error"]
    assert not (outside / "evil").exists()
    # wrong session, wrong manifest path, spill into the workspace path space
    reply, _ = _transfer(bulk.path, put_header("other", "workspace", "a.txt", b"x"), b"x")
    assert reply["ok"] is False and "session" in reply["error"]
    reply, _ = _transfer(bulk.path, put_header("s1", "manifest", "m.json", b"{}"), b"{}")
    assert reply["ok"] is False
    reply, _ = _transfer(bulk.path, put_header("s1", "workspace", ".spill/x", b"x"), b"x")
    assert reply["ok"] is False
    reply, _ = _transfer(bulk.path, put_header("s1", "spill", "r1.txt", b"x"), b"x")
    assert reply["ok"] is False and ".spill/" in reply["error"]     # spill needs the prefix
    assert bulk.puts == []


def test_bulk_put_over_the_limit_is_refused(session):
    ep = Endpoints(session.root, {"bulk": 5001})
    srv = BulkServer(ep.listener(5001), session.root, "s1", lambda n: [], max_bytes=10).start()
    try:
        reply, _ = _transfer(ep.path(5001), put_header("s1", "workspace", "a", b"x" * 11), b"x" * 11)
        assert reply["ok"] is False and "limit" in reply["error"]
    finally:
        srv.stop()
        ep.close()


def test_bulk_get_returns_the_acked_log_after_seq(bulk):
    sync = bulk.sync
    sync.accept(_ev(1, "session_start", {
        "user": "u", "tenant": "t", "workflow_run_id": "u/t/s1", "config_hash": HASH,
        "system_hash": HASH, "tools": [], "harness_version": "x", "image_version": "x",
        "resumed": False}))
    sync.accept(_ev(2, "input", {"input_id": "in_1", "kind": "task", "text": "hi",
                                 "dedupe": "new"}))
    sync.accept(_ev(4, "clock", {"kind": "turn_clock", "at": 1}))     # a gap: not stored
    reply, body = _transfer(bulk.path, get_header("s1", 0))
    assert reply["ok"] and reply["last_seq"] == 2 and reply["sha256"] == sha256_hex(body)
    lines = [json.loads(l) for l in body.decode().splitlines()]
    assert [e["seq"] for e in lines] == [1, 2] and lines[1]["body"]["input_id"] == "in_1"
    reply, body = _transfer(bulk.path, get_header("s1", 1))
    assert reply["last_seq"] == 2 and [json.loads(l)["seq"] for l in body.splitlines()] == [2]
    reply, body = _transfer(bulk.path, get_header("s1", 2))
    assert reply == {"ok": True, "bytes": 0, "sha256": sha256_hex(b""), "last_seq": 2}


# ---------------------------------------------------------------- base boot
@pytest.mark.asyncio
async def test_fresh_session_boots_from_the_base_snapshot(session):
    class Timed(FakeSandbox):
        def restore(self, session_dir, snapshot_dir=None):
            super().restore(session_dir, snapshot_dir)
            self.last_vm_event = {"op": "restore", "source": "base", "ms": 42}

    sandbox = Timed([{"text": "hi"}], base=True)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done"
    assert sandbox.restores == ["base"] and sandbox.starts == 0
    boot = host(w, "vm")[0]
    assert (boot["op"], boot["source"], boot["resume"]) == ("restore", "base", False)
    assert boot["ms"] == 42 and isinstance(boot["ready_ms"], int)   # the sandbox's own figure
    assert w.last_hello["resume"] is False
    await w.close()


# ---------------------------------------------------------------- park / resume
@pytest.mark.asyncio
async def test_park_mid_task_then_resume_delivers_ui_response_first(session):
    """Park while only a device call runs (honoured at once), snapshot; a
    device result and an answer arrive while parked; the wake restores the
    session snapshot, no new hello, ui_response before side_result, seq
    continues in the one run; heartbeats go on without reaching the guest."""
    dc = DeviceCalls()
    sandbox = FakeSandbox([CALL, {"text": "after park"}], exit_after_settle=False,
                          restore_delay=0.2)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=dc,
               **worker_kwargs(heartbeat_s=0.05))
    sink = w.attach("in_1")
    t1 = asyncio.ensure_future(w.act({"input_id": "in_1", "text": "calendar?"}))
    await until(lambda: dc.is_open("call_d"))
    guest = sandbox.guests[0]

    assert await w.park("question") is True
    assert w.vm_state == "parked" and not sandbox.is_alive()
    assert sandbox.snapshots == [str(session.root / "vm" / "snapshot")]
    assert guest.parks == 1 and w.parked_file.exists()
    ipc = session.root / "ipc"
    # no :5000/:5001 listener while parked: the idle-wait loop cannot reconnect
    assert not (ipc / "v.sock_5000").exists() and not (ipc / "v.sock_5001").exists()
    assert (ipc / "v.sock_5010").exists()
    types = [e["type"] for e in w.sync.read(0)]
    assert types[-2:] == ["checkpoint", "parked"]
    snap = [b for b in host(w, "vm") if b["op"] == "snapshot"]
    assert snap and snap[0]["source"] == "session" and snap[0]["ok"] is True
    assert any(b.get("kind") == "parked" and b["reason"] == "question" for b in host(w, "workflow"))
    n_controls = len(guest.controls)
    await asyncio.sleep(0.2)                                   # heartbeats while parked
    assert any(b.get("kind") == "heartbeat" and b.get("parked") for b in host(w, "workflow"))
    assert len(guest.controls) == n_controls

    # a device result, then the answer: both held, the wake sends the answer first
    assert dc.resolve("call_d", "[]") is True
    await asyncio.sleep(0)
    assert w.answer("q_1", "yes", "in_ans") == "held"
    r1 = await t1
    assert r1["outcome"] == "done" and r1["answer"] == "after park"
    after = [c["type"] for c in guest.controls[n_controls:]]
    assert after[:2] == ["ui_response", "side_result"], after
    assert [c["type"] for c in guest.controls].count("hello") == 1   # no new hello
    cseqs = [c["cseq"] for c in guest.controls]
    assert cseqs == list(range(cseqs[0], cseqs[0] + len(cseqs)))     # cseq continues
    assert sandbox.restores == ["session"] and len(sandbox.guests) == 1
    assert ("restore", "session") in vm_ops(w)
    assert not w.parked_file.exists() and w.vm_state == "running"
    assert (ipc / "v.sock_5000").exists() and (ipc / "v.sock_5001").exists()
    assert_one_contiguous_run(w)
    assert [e["seq"] for e in w.sync.read(0)] == [e["seq"] for e in guest.events]
    sse = await drain(sink)
    assert sse[-1] == DONE
    await w.close()


@pytest.mark.asyncio
async def test_redelivered_input_after_park_resume_runs_once(session):
    dc = DeviceCalls()
    sandbox = FakeSandbox([CALL, {"text": "ok"}], exit_after_settle=False)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=dc, **worker_kwargs())
    t1 = asyncio.ensure_future(w.act({"input_id": "in_1", "text": "hi"}))
    await until(lambda: dc.is_open("call_d"))
    assert await w.park("test") is True
    t2 = asyncio.ensure_future(w.act({"input_id": "in_1", "text": "hi"}))   # wakes the guest
    await until(lambda: w.vm_state == "running")
    await until(lambda: any(e["type"] == "input" and e["body"]["dedupe"] == "duplicate"
                            for e in w.sync.read(0)))
    dc.resolve("call_d", "[]")
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == r2 and r1["outcome"] == "done"
    inputs = [e["body"]["dedupe"] for e in w.sync.read(0) if e["type"] == "input"]
    assert inputs == ["new", "duplicate"]
    assert sum(1 for e in w.sync.read(0) if e["type"] == "settle") == 1
    assert sandbox.restores == ["session"]
    await w.close()


@pytest.mark.asyncio
async def test_idle_timer_parks_snapshots_destroys_and_the_next_task_restores(session):
    sandbox = FakeSandbox([{"text": "one"}], exit_after_settle=False)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs(session_idle_s=0.2))
    r1 = await w.act({"input_id": "in_1", "text": "hi"})
    assert r1["outcome"] == "done" and sandbox.is_alive()      # the guest outlives the settle
    await until(lambda: w.vm_state == "parked" and not sandbox.is_alive())
    await until(lambda: any(op == "destroy" for op, _ in vm_ops(w)))
    flow = [b.get("kind") for b in host(w, "workflow")]
    assert "idle" in flow and "parked" in flow
    ops = [op for op, _ in vm_ops(w)]
    assert ops[-2:] == ["snapshot", "destroy"]
    guest = sandbox.guests[0]
    seq_before = guest.seq
    # an input already settled is answered without waking the guest
    again = await w.act({"input_id": "in_1", "text": "hi"})
    assert again == r1 and sandbox.restores == [] and w.vm_state == "parked"
    # the next task restores the session snapshot: same guest, seq continues
    sandbox.script = [{"text": "two"}]
    guest.script = sandbox.script
    r2 = await w.act({"input_id": "in_2", "text": "again"})
    assert r2["outcome"] == "done" and r2["answer"] == "two"
    assert sandbox.restores == ["session"] and len(sandbox.guests) == 1
    assert [c["type"] for c in guest.controls].count("hello") == 1
    assert min(e["seq"] for e in guest.events if e["seq"] > seq_before) == seq_before + 1
    assert_one_contiguous_run(w)
    assert w.sync.seen_inputs == {"in_1", "in_2"}
    await w.close()


@pytest.mark.asyncio
async def test_a_restarted_worker_resumes_the_parked_session(session):
    """vm/parked.json carries the cseq: a new Worker (agent-api restart)
    restores the snapshot and the guest's handshake ack is accepted."""
    sandbox = FakeSandbox([{"text": "one"}], exit_after_settle=False)
    w1 = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
                **worker_kwargs())
    await w1.act({"input_id": "in_1", "text": "hi"})
    assert await w1.park("idle") is True
    cseq = json.loads(w1.parked_file.read_text())["cseq"]
    await w1.close()
    sandbox.script = sandbox.guests[0].script = [{"text": "two"}]
    w2 = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
                **worker_kwargs())
    assert w2.vm_state == "parked" and w2._outbox.highest_sent == cseq
    r2 = await w2.act({"input_id": "in_2", "text": "again"})
    assert r2["answer"] == "two" and sandbox.restores == ["session"]
    task = [c for c in sandbox.guests[0].controls if c["type"] == "task"][-1]
    assert task["cseq"] == cseq + 1
    assert_one_contiguous_run(w2)
    await w2.close()


@pytest.mark.asyncio
async def test_park_without_a_snapshot_falls_back_to_base_restore_and_log_return(session):
    class NoSnap(FakeSandbox):
        def snapshot(self, snapshot_dir):
            self.guest.parked.wait(5)
            raise RuntimeError("no KVM")

    sandbox = NoSnap([{"text": "one"}], exit_after_settle=False, base=True)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    await w.act({"input_id": "in_1", "text": "hi"})
    assert await w.park("idle") is True and w.vm_state == "parked"
    snap = [b for b in host(w, "vm") if b["op"] == "snapshot"][0]
    assert snap["ok"] is False and "no KVM" in snap["error"]
    sandbox.script = [{"text": "two"}]
    r2 = await w.act({"input_id": "in_2", "text": "again"})
    assert r2["answer"] == "two"
    assert sandbox.restores == ["base", "base"]
    g2 = sandbox.guests[1]
    assert g2.hello["resume"] is True and g2.returned is not None
    assert_one_contiguous_run(w)
    await w.close()


# ---------------------------------------------------------------- death
@pytest.mark.asyncio
async def test_death_mid_call_resumes_from_base_with_log_return_and_lost_call(session):
    dc = DeviceCalls()
    sandbox = FakeSandbox([CALL, {"text": "never"}], base=True, die_runs=1)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=dc, **worker_kwargs())
    sink = w.attach("in_1")
    result = await w.act({"input_id": "in_1", "text": "calendar?"})
    sse = await drain(sink)
    assert result["outcome"] == "done" and result["answer"] == "resumed answer"
    assert sse[-1] == DONE and not any(s.startswith("event: error") for s in sse)
    g0, g1 = sandbox.guests
    assert sandbox.restores == ["base", "base"]
    assert g1.hello["resume"] is True
    # cseq continues: the resumed hello is numbered after the first guest's controls
    assert g1.controls[0]["type"] == "hello"
    assert g1.controls[0]["cseq"] == g0.controls[-1]["cseq"] + 1
    # the log return: every acked event of the dead guest, then the seq goes on
    assert w.bulk.served and w.bulk.served[0]["op"] == "get"
    assert [e["seq"] for e in g1.returned] == [e["seq"] for e in g0.events]
    assert g1.events[0]["seq"] == g0.events[-1]["seq"] + 1
    assert_one_contiguous_run(w)
    lost = [e for e in w.sync.read(0) if e["type"] == "op_state" and e["body"]["state"] == "lost"]
    assert [e["body"]["call_id"] for e in lost] == ["call_d"]
    tr = [e for e in w.sync.read(0) if e["type"] == "tool_result"]
    assert tr[0]["body"]["content"] == LOST_TEXT
    # the host gave up on the call: a late device result is a 409, a late
    # side_result is dropped and logged input{dedupe: late}
    assert not dc.is_open("call_d") and dc.resolve("call_d", "late") is False
    assert any(b.get("state") == "lost" for b in host(w, "side_call"))
    assert w.send_control("side_result", {"call_id": "call_d", "side": "device",
                                          "content": "late", "ok": True}) == "dropped"
    assert {"kind": "side_result", "call_id": "call_d", "side": "device",
            "dedupe": "late"} in host(w, "input")
    assert any(b.get("kind") == "resume" and b["reason"] == "guest_died"
               for b in host(w, "workflow"))
    assert [op for op, src in vm_ops(w) if op == "restore"] == ["restore", "restore"]
    await w.close()


@pytest.mark.asyncio
async def test_death_without_base_snapshot_falls_back_to_rootfs_boot(session):
    sandbox = FakeSandbox([CALL, {"text": "never"}], die_runs=1)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    result = await w.act({"input_id": "in_1", "text": "calendar?"})
    assert result["outcome"] == "done" and sandbox.starts == 2
    assert vm_ops(w)[:2] == [("boot", "rootfs"), ("boot", "rootfs")]
    assert sandbox.guests[1].hello["resume"] is True
    assert_one_contiguous_run(w)
    await w.close()


@pytest.mark.asyncio
async def test_second_death_in_a_task_fails_it(session):
    sandbox = FakeSandbox([CALL, {"text": "never"}], base=True, die_runs=2)
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    sink = w.attach("in_1")
    result = await w.act({"input_id": "in_1", "text": "calendar?"})
    sse = await drain(sink)
    assert result["outcome"] == "failed" and "died again" in result["answer"]
    assert any(s.startswith("event: error") for s in sse) and sse[-1] == DONE
    assert len(sandbox.guests) == 2                          # max_resumes = 1
    assert session.read_history()[-1]["role"] == "assistant"
    # the next task starts fresh (a new run); its resume budget is its own
    sandbox.die_runs = 0
    sandbox.script = [{"text": "fresh"}]
    r2 = await w.act({"input_id": "in_2", "text": "again"})
    assert r2["answer"] == "fresh" and w.sync.runs() == [1, 2]
    await w.close()
