"""The group-commit writer (app.host.durable) under StateSync and HostLog:
ordering, ack only after durable, crash safety (nothing acked that is not
on disk, no torn line, no hole), concurrency with the gateway-events route."""
from __future__ import annotations

import json
import os
import threading

import pytest

from protocol.fold import fold, state_hash

from app.host.durable import GroupWriter, PoisonedError
from app.host.state_sync import HostLog, StateSync, StateSyncError
from host_fakes import (FakeRegistry, FakeSandbox, FakeTenant, read_jsonl, session,  # noqa: F401
                        users_root, worker_kwargs)
from test_host_state_sync import start, text_task


class GatedFsync:
    """An fsync that waits for `open_gate` (and can fail on demand)."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.gate.set()
        self.fail: OSError | None = None
        self.calls = 0
        self.entered = threading.Event()

    def __call__(self, fd: int) -> None:
        self.entered.set()
        self.gate.wait(10)
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        os.fsync(fd)


def test_one_path_keeps_its_order_across_threads(tmp_path):
    writer = GroupWriter(shards=2)
    path = tmp_path / "log.jsonl"

    def put(tag: str) -> list:
        return [writer.append(path, f"{tag}-{i}\n".encode()) for i in range(200)]

    futs: dict[str, list] = {}
    threads = [threading.Thread(target=lambda t=t: futs.__setitem__(t, put(t))) for t in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for fs in futs.values():
        for f in fs:
            assert isinstance(f.result(5), int)          # the inode, once fsynced
    lines = path.read_text().splitlines()
    assert len(lines) == 800
    for tag in "abcd":
        mine = [int(line.split("-")[1]) for line in lines if line.startswith(tag)]
        assert mine == list(range(200))                  # in the order appended


def test_batches_share_one_fsync(tmp_path):
    fsync = GatedFsync()
    writer = GroupWriter(shards=1, fsync=fsync)
    fsync.gate.clear()
    first = writer.append(tmp_path / "a", b"0\n")
    assert fsync.entered.wait(5)                         # the writer blocks in its fsync
    rest = [writer.append(tmp_path / "a", f"{i}\n".encode()) for i in range(1, 50)]
    fsync.gate.set()
    for f in [first, *rest]:
        f.result(5)
    assert fsync.calls == 2          # 50 records: the first alone, the 49 queued meanwhile in one
    assert (tmp_path / "a").read_text().split() == [str(i) for i in range(50)]


def test_ack_only_after_durable(session):
    fsync = GatedFsync()
    sync = StateSync(session, writer=GroupWriter(shards=1, fsync=fsync))
    sync.accept(start())
    fsync.gate.clear()
    events = text_task()
    fut = sync.submit(events[1])
    assert fut is not None and not fut.done()
    assert sync.state.last_seq == 2                      # folded
    assert sync.last_seq == 1 and sync.read(0) == [start()]   # not acked, not returned
    assert sync.submit(events[1]) is None                # a resend: nothing new
    fsync.gate.set()
    fut.result(5)
    sync.stored(fut)
    assert sync.last_seq == 2 and [e["seq"] for e in sync.read(0)] == [1, 2]


def test_a_failed_write_acks_nothing_leaves_no_torn_line_and_the_resend_is_accepted(session):
    fsync = GatedFsync()
    sync = StateSync(session, writer=GroupWriter(shards=1, fsync=fsync))
    events = text_task()
    for e in events[:3]:
        sync.accept(e)
    seg = session.root / "events" / "r0001" / "000001.jsonl"
    before = seg.read_bytes()
    fsync.fail = OSError(28, "No space left on device")
    with pytest.raises(StateSyncError):
        sync.accept(events[3])
    fsync.fail = None
    assert sync.last_seq == 3
    assert seg.read_bytes() == before                    # cut back: no torn line
    # the failure poisoned the log: the next submit refolds from disk first
    with pytest.raises(StateSyncError, match="could not be written"):
        sync.submit(events[3])
    assert sync.state.last_seq == 3 and sync.last_seq == 3
    for e in events[3:]:                                 # the guest's resend
        sync.accept(e)
    assert sync.last_seq == 6
    assert state_hash(fold(sync.read(0))) == state_hash(sync.state)
    assert StateSync(session).last_seq == 6


def test_poisoned_group_fails_later_records_of_any_path(tmp_path):
    fsync = GatedFsync()
    writer = GroupWriter(shards=1, fsync=fsync)
    group = object.__new__(type("G", (), {}))
    fsync.gate.clear()
    fsync.fail = OSError(5, "EIO")
    a = writer.append(tmp_path / "000001.jsonl", b"1\n", group=group)
    b = writer.append(tmp_path / "000002.jsonl", b"2\n", group=group)
    fsync.gate.set()
    with pytest.raises(OSError):
        a.result(5)
    with pytest.raises(OSError):
        b.result(5)
    fsync.fail = None
    with pytest.raises(PoisonedError):
        writer.append(tmp_path / "000003.jsonl", b"3\n", group=group).result(5)
    writer.reset(group)
    writer.append(tmp_path / "000003.jsonl", b"3\n", group=group).result(5)
    assert not (tmp_path / "000002.jsonl").exists()       # never written


def test_host_log_hseq_is_gapless_with_the_gateway_route_in_threads(tmp_path):
    """The loop's host events (StateSync.host_event, not waited for) and
    the gateway-events route (append_durable from its threads) share one
    counter: every hseq once, in file order."""
    from app.gateway.events import record_gateway_event
    from app.session.dir_store import DirStore
    store = DirStore(tmp_path / "sessions")
    s = store.get_or_create("u", "t", "s1")
    sync = StateSync(s)
    payload = {"user": "u", "tenant": "t", "session_id": "s1", "op": "call"}

    def gateway():
        for _ in range(50):
            assert record_gateway_event(dict(payload), sessions=store)

    threads = [threading.Thread(target=gateway) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(100):
        sync.host_event("workflow", {"kind": "heartbeat"})
    for t in threads:
        t.join()
    host = sync.read_host()
    assert [h["hseq"] for h in host] == list(range(1, 301))
    assert sum(h["type"] == "gateway" for h in host) == 200


def test_host_log_append_returns_before_the_fsync(tmp_path):
    fsync = GatedFsync()
    log = HostLog(tmp_path / "events", writer=GroupWriter(shards=1, fsync=fsync))
    log.append("s", "vm", {"op": "boot"})            # counted from disk, then written
    log.flush(5)
    fsync.gate.clear()
    rec = log.append("s", "vm", {"op": "boot"})       # does not wait
    assert rec["hseq"] == 2
    fsync.gate.set()
    log.flush(5)
    assert [r["hseq"] for r in read_jsonl(tmp_path / "events" / "host-000001.jsonl")] == [1, 2]


@pytest.mark.asyncio
async def test_worker_never_acks_a_seq_that_is_not_on_disk(session):
    """Every ack the Worker sends names a seq whose line is already in the
    run's segment (read back from the file at the moment of the ack)."""
    from app.host.sides import DeviceCalls
    from app.host.worker import Worker
    sandbox = FakeSandbox([{"call": {"id": "c1", "name": "bash", "args": {}}},
                           {"text": "Hello there world"}])
    w = Worker(session, FakeTenant(), FakeRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs())
    acks: list[tuple[int, int]] = []
    send = w._send_raw

    def checked(msg):
        if "ack_seq" in msg:
            run = session.root / "events" / f"r{w.sync.run:04d}"
            on_disk = sum(len(p.read_text().splitlines()) for p in run.glob("*.jsonl")) \
                if run.exists() else 0
            acks.append((msg["ack_seq"], on_disk))
        send(msg)
    w._send_raw = checked
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done"
    assert acks and all(seq <= on_disk for seq, on_disk in acks)
    assert max(seq for seq, _ in acks) == w.sync.last_seq
    lines = [json.loads(x) for x in (session.root / "events" / "r0001" / "000001.jsonl")
             .read_text().splitlines()]
    assert [e["seq"] for e in lines] == list(range(1, w.sync.last_seq + 1))
    await w.close()
