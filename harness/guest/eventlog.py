"""eventlog — the guest's local event buffer and the log-writer thread
(the host is the canonical store; docs/GUEST.md).

EventLog(dir)
  JSONL segments `events/000001.jsonl`, `events/000002.jsonl`, ... of at most
  SEGMENT_EVENTS (protocol.limits, 1000) events each, named and read through
  protocol.replay (segment_name, segments, iter_segment); a new segment opens when the current one
  is full (or after rotate()). append(event) writes one line, flushes and
  fsyncs; the event must be the next seq. extend(events) writes several and
  fsyncs once per segment. mark_acked(seq) records the host's
  ack in the marker file `events/acked` (protocol.fsio.atomic_write) so a restarted
  guest knows what the host already holds. read(after_seq=0) returns the
  events in seq order. On open, last_seq and acked_seq are recovered from
  the files.

LogWriter(eventlog, transport)
  The thread the coordinator hands events to: put(event) queues it; the
  thread drains the queue in batches, extend(batch) (one fsync per batch),
  then transport.send_event per event; flush(timeout) waits until
  everything put so far has been written and handed to the transport;
  mark_acked(seq) records the host's ack in memory and the thread writes the
  marker at most every ACK_MARKER_INTERVAL_MS (and on flush()/stop());
  stop() ends the thread after draining.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path

from protocol.canonical import is_int
from protocol.fsio import atomic_write
from protocol.limits import SEGMENT_EVENTS
from protocol.messages import encode
from protocol.replay import iter_segment, segment_name, segments

log = logging.getLogger("guest.eventlog")

ACKED_MARKER = "acked"
ACK_MARKER_INTERVAL_MS = 200   # LogWriter: the acked marker is written at most this often


def _torn_tail(path: Path) -> bool:
    """True when `path` is non-empty and does not end with a newline."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


class EventLog:
    def __init__(self, dir, segment_events: int = SEGMENT_EVENTS):
        if not is_int(segment_events, 1):
            raise ValueError(f"segment_events must be a positive int, got {segment_events!r}")
        self.dir = Path(dir)
        self.events_dir = self.dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.segment_events = segment_events
        self._lock = threading.RLock()
        self._segment = 0          # index of the current segment (0 = none yet)
        self._count = 0            # events in the current segment
        self._fh = None
        self._last_seq = 0
        self._acked = 0
        self._recover()

    # ------------------------------------------------------------ properties

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def acked_seq(self) -> int:
        return self._acked

    @property
    def marker_path(self) -> Path:
        return self.events_dir / ACKED_MARKER

    def segments(self) -> list[Path]:
        """Segment files in order."""
        return segments(self.events_dir)

    # ------------------------------------------------------------ recovery

    def _recover(self) -> None:
        segs = self.segments()
        if segs:
            last = segs[-1]
            count, last_seq = 0, 0
            for ev in self._read_segment(last):
                count += 1
                last_seq = ev["seq"]
            if count == 0 and len(segs) > 1:
                prev = list(self._read_segment(segs[-2]))
                last_seq = prev[-1]["seq"] if prev else 0
            self._segment = int(last.stem)
            self._count = count
            self._last_seq = last_seq
        try:
            text = self.marker_path.read_text(encoding="utf-8").strip()
            self._acked = int(text) if text else 0
        except (FileNotFoundError, ValueError):
            self._acked = 0

    @staticmethod
    def _read_segment(path: Path):
        return iter_segment(path, on_bad=lambda p, lineno, exc: log.error(
            "corrupt line %d in %s ignored", lineno, p))

    # ------------------------------------------------------------ writing

    def _open_next(self) -> None:
        if self._fh is not None:
            self._sync()
            self._fh.close()
        self._segment += 1
        self._count = 0
        path = self.events_dir / segment_name(self._segment)
        self._fh = path.open("a", encoding="utf-8")

    def append(self, event: dict) -> None:
        """Write one event (the next seq). Durable on disk when this returns."""
        self.extend([event])

    def extend(self, events: list[dict]) -> None:
        """Write events (consecutive, starting at the next seq), fsyncing
        once per segment instead of per event. Durable when this returns; on
        a bad seq the events before it are written."""
        with self._lock:
            try:
                for event in events:
                    self._write(event)
            finally:
                if self._fh is not None:
                    self._sync()

    def _write(self, event: dict) -> None:
        seq = event.get("seq") if isinstance(event, dict) else None
        if not is_int(seq, 1):
            raise ValueError(f"event.seq must be a positive int, got {seq!r}")
        if seq != self._last_seq + 1:
            raise ValueError(f"event seq {seq} out of order: expected {self._last_seq + 1}")
        if self._count >= self.segment_events or self._segment == 0:
            self._open_next()
        elif self._fh is None:      # reopened after a restart or close(): continue it
            path = self.events_dir / segment_name(self._segment)
            self._fh = path.open("a", encoding="utf-8")
            if _torn_tail(path):   # a crash mid-write left a partial line: end it
                self._fh.write("\n")
        self._fh.write(encode(event))
        self._count += 1
        self._last_seq = seq

    def _sync(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def rotate(self) -> None:
        """Close the current segment; the next append starts a new one."""
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            if self._count:
                self._count = self.segment_events   # force a new segment

    def mark_acked(self, seq: int) -> None:
        """Record that the host holds everything through `seq`. Monotonic;
        an ack beyond last_seq is a protocol error (ValueError)."""
        if not is_int(seq, 0):
            raise ValueError(f"acked seq must be a non-negative int, got {seq!r}")
        with self._lock:
            if seq > self._last_seq:
                raise ValueError(f"ack {seq} beyond last appended seq {self._last_seq}")
            if seq <= self._acked:
                return
            atomic_write(self.marker_path, f"{seq}\n".encode())
            self._acked = seq

    # ------------------------------------------------------------ reading

    def read(self, after_seq: int = 0) -> list[dict]:
        """Every event with seq > after_seq, in order."""
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
            out: list[dict] = []
            for path in self.segments():
                for ev in self._read_segment(path):
                    if ev.get("seq", 0) > after_seq:
                        out.append(ev)
            return out

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------- log writer


class LogWriter:
    """Appends events to the EventLog, then hands them to the transport.
    One thread; put() never blocks the caller (the coordinator).

    Batching: the thread blocks for one event, then drains whatever else is
    queued and writes the batch with one EventLog.extend (one fsync per
    batch); the events then go to the transport one by one, in seq order.

    Ack marker: mark_acked(seq) only records the highest ack in memory; the
    thread writes it to the marker file (EventLog.mark_acked) at most every
    ACK_MARKER_INTERVAL_MS, clamped to what is on disk (an ack beyond the
    appended seq is applied once that seq is written), and flush() / stop()
    write it before returning."""

    def __init__(self, eventlog: EventLog, transport):
        self.eventlog = eventlog
        self.transport = transport
        self._q: queue.Queue = queue.Queue()
        self._cv = threading.Condition()
        self._put = 0
        self._done = 0
        self._ack_pending = 0            # highest ack seen (mark_acked), not yet on disk
        self._ack_written_at = 0.0       # monotonic time of the last marker write
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("log writer already started")
        self._thread = threading.Thread(target=self._run, name="guest-log-writer", daemon=True)
        self._thread.start()

    def put(self, event: dict) -> None:
        with self._cv:
            self._put += 1
        self._q.put(event)

    def mark_acked(self, seq: int) -> None:
        """Record the host's ack; the writer thread persists it (coalesced)."""
        if not is_int(seq, 0):
            raise ValueError(f"acked seq must be a non-negative int, got {seq!r}")
        with self._cv:
            self._ack_pending = max(self._ack_pending, seq)

    def sync_acked(self) -> None:
        """Write the pending ack marker now (clamped to the appended seq)."""
        with self._cv:
            target = self._ack_pending
        seq = min(target, self.eventlog.last_seq)
        if seq <= self.eventlog.acked_seq:
            return
        try:
            self.eventlog.mark_acked(seq)
        except Exception as exc:  # noqa: BLE001 - a marker problem is not a loop problem
            log.exception("acked marker write failed for seq %s", seq)
            self.errors.append(f"mark_acked {seq}: {exc}")
        self._ack_written_at = time.monotonic()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until every event put so far is on disk and handed to the
        transport (and write the pending ack marker). False on timeout."""
        with self._cv:
            target = self._put
            ok = self._cv.wait_for(lambda: self._done >= target, timeout)
        if ok:
            self.sync_acked()
        return ok

    def stop(self, timeout: float | None = 5.0) -> None:
        if self._thread is None:
            return
        self._q.put(None)
        self._thread.join(timeout)
        self.sync_acked()

    def _ack_due(self) -> bool:
        return (self._ack_pending > self.eventlog.acked_seq
                and min(self._ack_pending, self.eventlog.last_seq) > self.eventlog.acked_seq
                and (time.monotonic() - self._ack_written_at) * 1000 >= ACK_MARKER_INTERVAL_MS)

    def _run(self) -> None:
        wait_s = ACK_MARKER_INTERVAL_MS / 1000.0
        while True:
            try:
                first = self._q.get(timeout=wait_s)
            except queue.Empty:
                if self._ack_due():
                    self.sync_acked()
                continue
            batch, stopping = [], first is None
            if first is not None:
                batch.append(first)
            while not stopping:
                try:
                    event = self._q.get_nowait()
                except queue.Empty:
                    break
                if event is None:
                    stopping = True
                else:
                    batch.append(event)
            if batch:
                self._write(batch)
                for event in batch:
                    try:
                        self.transport.send_event(event)
                    except Exception as exc:
                        log.exception("transport refused seq %s", event.get("seq"))
                        self.errors.append(f"send seq {event.get('seq')}: {exc}")
                with self._cv:
                    self._done += len(batch)
                    self._cv.notify_all()
            if self._ack_due():
                self.sync_acked()
            if stopping:
                return

    def _write(self, batch: list) -> None:
        """One extend (one fsync); on an error the events after the last one
        written are retried one by one, so one bad event loses only itself."""
        before = self.eventlog.last_seq
        try:
            self.eventlog.extend(batch)
            return
        except Exception:  # noqa: BLE001 - fall back to per-event appends below
            pass
        # extend writes a prefix of the batch: retry the rest one by one
        for event in batch[self.eventlog.last_seq - before:]:
            try:
                self.eventlog.append(event)
            except Exception as exc:  # disk trouble: the host is the canonical store
                seq = event.get("seq") if isinstance(event, dict) else None
                log.exception("event log append failed for seq %s", seq)
                self.errors.append(f"append seq {seq}: {exc}")
