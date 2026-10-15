"""guest.eventlog — segments, acked marker, recovery, the log-writer thread."""
import threading
import time

import pytest

from guest import eventlog as eventlog_mod
from guest.eventlog import ACKED_MARKER, SEGMENT_EVENTS, EventLog, LogWriter, segment_name
from protocol.events import make_event

SID = "sess-log"

# the buffer is tested with bare clock events, not session logs
pytestmark = pytest.mark.no_log_verify("synthetic clock-only segments")


def ev(seq, body=None):
    return make_event(SID, seq, f"2026-10-07T00:00:{seq % 60:02d}Z", "clock",
                      body if body is not None else {"kind": "turn_clock", "at": seq})


def fill(log, start, stop):
    for seq in range(start, stop + 1):
        log.append(ev(seq))


# ---------------------------------------------------------------- append / read


def test_append_and_read(tmp_path):
    log = EventLog(tmp_path)
    assert log.last_seq == 0 and log.acked_seq == 0
    fill(log, 1, 3)
    assert log.last_seq == 3
    assert [e["seq"] for e in log.read()] == [1, 2, 3]
    assert [e["seq"] for e in log.read(after_seq=2)] == [3]
    assert log.read(after_seq=3) == []
    assert log.read()[0] == ev(1)
    assert (tmp_path / "events" / "000001.jsonl").exists()


def test_lines_are_jsonl(tmp_path):
    log = EventLog(tmp_path)
    fill(log, 1, 2)
    text = (tmp_path / "events" / "000001.jsonl").read_text()
    lines = text.splitlines()
    assert len(lines) == 2 and text.endswith("\n")
    assert lines[0].startswith('{"session_id":"sess-log","seq":1,')


def test_append_out_of_order_rejected(tmp_path):
    log = EventLog(tmp_path)
    log.append(ev(1))
    with pytest.raises(ValueError, match="out of order: expected 2"):
        log.append(ev(3))
    with pytest.raises(ValueError, match="out of order"):
        log.append(ev(1))
    with pytest.raises(ValueError, match="event.seq"):
        log.append({"type": "clock", "body": {}})
    assert log.last_seq == 1


# ---------------------------------------------------------------- rotation


def test_rotates_at_segment_size(tmp_path):
    log = EventLog(tmp_path, segment_events=3)
    fill(log, 1, 7)
    names = [p.name for p in log.segments()]
    assert names == [segment_name(1), segment_name(2), segment_name(3)]
    counts = [len(p.read_text().splitlines()) for p in log.segments()]
    assert counts == [3, 3, 1]
    assert [e["seq"] for e in log.read()] == list(range(1, 8))
    assert [e["seq"] for e in log.read(after_seq=5)] == [6, 7]


def test_default_segment_size_is_1000(tmp_path):
    assert SEGMENT_EVENTS == 1000
    assert EventLog(tmp_path).segment_events == 1000
    with pytest.raises(ValueError):
        EventLog(tmp_path, segment_events=0)


def test_explicit_rotate(tmp_path):
    log = EventLog(tmp_path, segment_events=10)
    fill(log, 1, 2)
    log.rotate()
    fill(log, 3, 4)
    assert [p.name for p in log.segments()] == ["000001.jsonl", "000002.jsonl"]
    assert [e["seq"] for e in log.read()] == [1, 2, 3, 4]
    log.rotate()
    log.rotate()                      # idle rotates do not create empty segments
    assert len(log.segments()) == 2


# ---------------------------------------------------------------- acked marker


def test_acked_marker(tmp_path):
    log = EventLog(tmp_path)
    fill(log, 1, 5)
    marker = tmp_path / "events" / ACKED_MARKER
    assert not marker.exists()
    log.mark_acked(3)
    assert log.acked_seq == 3
    assert marker.read_text().strip() == "3"
    log.mark_acked(2)                 # stale: no regression
    assert log.acked_seq == 3
    log.mark_acked(5)
    assert marker.read_text().strip() == "5"
    with pytest.raises(ValueError, match="beyond last appended"):
        log.mark_acked(6)
    with pytest.raises(ValueError):
        log.mark_acked(-1)


def test_restart_recovers_last_seq_and_acked(tmp_path):
    log = EventLog(tmp_path, segment_events=2)
    fill(log, 1, 5)
    log.mark_acked(4)
    log.close()

    again = EventLog(tmp_path, segment_events=2)
    assert again.last_seq == 5
    assert again.acked_seq == 4
    assert [e["seq"] for e in again.read(after_seq=again.acked_seq)] == [5]
    again.append(ev(6))               # continues the open segment
    assert [p.name for p in again.segments()] == ["000001.jsonl", "000002.jsonl",
                                                  "000003.jsonl"]
    assert len((tmp_path / "events" / "000003.jsonl").read_text().splitlines()) == 2
    with pytest.raises(ValueError):
        again.append(ev(6))


def test_restart_on_full_segment_opens_next(tmp_path):
    log = EventLog(tmp_path, segment_events=2)
    fill(log, 1, 2)
    log.close()
    again = EventLog(tmp_path, segment_events=2)
    again.append(ev(3))
    assert [p.name for p in again.segments()] == ["000001.jsonl", "000002.jsonl"]


# ---------------------------------------------------------------- log writer


class SpyTransport:
    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)
        self.lock = threading.Lock()

    def send_event(self, event):
        if event["seq"] in self.fail_on:
            raise RuntimeError("link refused")
        with self.lock:
            self.sent.append(event)


def test_log_writer_appends_then_sends(tmp_path):
    log = EventLog(tmp_path)
    transport = SpyTransport()
    writer = LogWriter(log, transport)
    writer.start()
    for seq in range(1, 6):
        writer.put(ev(seq))
    assert writer.flush(timeout=5)
    assert log.last_seq == 5
    assert [e["seq"] for e in transport.sent] == [1, 2, 3, 4, 5]
    writer.mark_acked(5)
    assert writer.flush(timeout=5)           # flush writes the pending marker
    assert log.acked_seq == 5
    writer.stop()
    assert writer.errors == []


def test_log_writer_keeps_going_after_transport_error(tmp_path):
    log = EventLog(tmp_path)
    transport = SpyTransport(fail_on={2})
    writer = LogWriter(log, transport)
    writer.start()
    for seq in range(1, 4):
        writer.put(ev(seq))
    assert writer.flush(timeout=5)
    assert log.last_seq == 3                       # the log never loses an event
    assert [e["seq"] for e in transport.sent] == [1, 3]
    assert writer.errors and "seq 2" in writer.errors[0]
    writer.stop()


def test_log_writer_flush_times_out_when_not_started(tmp_path):
    writer = LogWriter(EventLog(tmp_path), SpyTransport())
    writer.put(ev(1))
    assert writer.flush(timeout=0.05) is False
    assert writer.flush(timeout=0.05) is False
    writer.start()
    assert writer.flush(timeout=5) is True
    writer.stop()


class CountingLog(EventLog):
    """Counts extend() calls (one fsync each) and marker writes."""

    def __init__(self, *a, **kw):
        self.extends: list = []
        self.marks: list = []
        super().__init__(*a, **kw)

    def extend(self, events):
        self.extends.append([e["seq"] for e in events])
        super().extend(events)

    def mark_acked(self, seq):
        self.marks.append(seq)
        super().mark_acked(seq)


class GatedTransport(SpyTransport):
    """Blocks the first send until released, so later puts queue up."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def send_event(self, event):
        self.gate.wait(5)
        super().send_event(event)


def test_log_writer_batches_queued_events_into_one_extend(tmp_path):
    log = CountingLog(tmp_path)
    transport = GatedTransport()
    writer = LogWriter(log, transport)
    writer.start()
    writer.put(ev(1))                          # the thread takes it and blocks in send
    for seq in range(2, 51):
        writer.put(ev(seq))
    transport.gate.set()
    assert writer.flush(timeout=5)
    assert [e["seq"] for e in transport.sent] == list(range(1, 51))
    assert [s for batch in log.extends for s in batch] == list(range(1, 51))
    assert len(log.extends) <= 3               # 1, then the 49 queued behind it
    assert log.read() == [ev(s) for s in range(1, 51)]
    writer.stop()
    assert writer.errors == []


def test_log_writer_batch_with_a_bad_event_still_writes_and_sends_the_rest(tmp_path):
    log = EventLog(tmp_path)
    transport = GatedTransport()
    writer = LogWriter(log, transport)
    writer.start()
    writer.put(ev(1))
    for e in (ev(2), ev(2), ev(3)):            # a duplicate seq in the middle of a batch
        writer.put(e)
    transport.gate.set()
    assert writer.flush(timeout=5)
    assert log.last_seq == 3
    assert [e["seq"] for e in transport.sent] == [1, 2, 2, 3]
    assert len(writer.errors) == 1 and "seq 2" in writer.errors[0]
    writer.stop()


def test_log_writer_coalesces_ack_marker_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog_mod, "ACK_MARKER_INTERVAL_MS", 60_000)
    log = CountingLog(tmp_path)
    writer = LogWriter(log, SpyTransport())
    writer.start()
    for seq in range(1, 21):
        writer.put(ev(seq))
    assert writer.flush(timeout=5)
    for seq in range(1, 21):
        writer.mark_acked(seq)
    assert log.marks == []                     # nothing per ack
    assert writer.flush(timeout=5)
    assert log.acked_seq == 20 and (tmp_path / "events" / ACKED_MARKER).read_text() == "20\n"
    assert len(log.marks) == 1
    writer.stop()


def test_log_writer_writes_a_pending_marker_on_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog_mod, "ACK_MARKER_INTERVAL_MS", 20)
    log = EventLog(tmp_path)
    writer = LogWriter(log, SpyTransport())
    writer.start()
    writer.put(ev(1))
    assert writer.flush(timeout=5)
    writer.mark_acked(1)
    deadline = time.time() + 5
    while log.acked_seq != 1 and time.time() < deadline:
        time.sleep(0.01)
    assert log.acked_seq == 1                  # no event, no flush: the thread wrote it
    writer.stop()


def test_log_writer_ack_beyond_the_appended_seq_applies_once_written(tmp_path):
    log = EventLog(tmp_path)
    writer = LogWriter(log, SpyTransport())
    writer.mark_acked(3)                       # not appended yet: no error, nothing written
    writer.sync_acked()
    assert log.acked_seq == 0
    writer.start()
    for seq in (1, 2):
        writer.put(ev(seq))
    assert writer.flush(timeout=5)
    assert log.acked_seq == 2                  # clamped to what is on disk
    writer.put(ev(3))
    writer.stop()                              # stop drains, then writes the marker
    assert log.acked_seq == 3
    assert EventLog(tmp_path).acked_seq == 3
    with pytest.raises(ValueError):
        writer.mark_acked(-1)
