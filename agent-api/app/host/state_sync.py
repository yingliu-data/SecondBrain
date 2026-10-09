"""state_sync — the canonical store of a session's events (PLAN.md 1, 4.2, 5).

  StateSync(session_dir)
    .accept(event) -> ack_seq     validate, append to events/NNNNNN.jsonl, fsync,
                                  fold into .state, project memory, THEN
                                  return the seq to ack (durable = host ack after
                                  append). Duplicates (seq <= last_seq) and gaps
                                  (seq > last_seq + 1) are not appended; the ack
                                  is the highest contiguous seq either way.
    .host_event(type, body)       the worker's own log: events/host-NNNNNN.jsonl
                                  (through host_log(events_dir): one writer per
                                  directory in the process)
    .read(after_seq=0)            events from disk, in seq order (safe from another
                                  thread: the bulk server's log return)
  read_current_run(events_dir, after_seq)   the same without loading the session
    .state                        protocol.fold.State folded from the log
    .question_bodies              question_id -> the `question` body (current run)
    .last_seq                     highest stored seq

Segments hold HARNESS_SEGMENT_EVENTS events each (seq 1..N in 000001.jsonl,
N+1..2N in 000002.jsonl), so the segment of a seq is a function of the seq.

Runs. A fresh guest (hello{resume: false}) begins with its own
`session_start` at seq 1, so it opens a new run; a resumed guest (a parked
snapshot restored, or a death resume with the log return, M3) continues the
current run's seq. Each run is therefore its own log under
`events/rNNNN/` (segments inside) with its own folded State; `begin_run()`
opens the next one. What must hold across runs - the input_ids seen and the
settle of each task - is folded from every run at load (`seen_inputs`,
`settled`), so a redelivered input is answered without a guest. The host
log `events/host-NNNNNN.jsonl` is one per session, across runs. With one
long-lived guest per session (M3) there is one run and the layout is the
plan's `events/000001.jsonl` one directory down.

Projections. The event log is the only record of the conversation and the
trace: the `history.jsonl` / `logs/trace.jsonl` projections retired in M6
(readers fold the log: app.session.eventlog). What remains is the memory
projection below.

Migrated runs (M6-CONTRACTS section 4). `migrated_run` is True while the
current run is one `app.session.migrate --events` wrote from a legacy
history (its session_start has `migrated: true`) and no guest has continued
it yet (it still ends with the migration's checkpoint). The worker's first
boot of such a session is a resume: `retool_migrated(tools)` re-pins the
run's session_start.tools to the hello's effective tools (the legacy run
never had a guest; its own allowlist holds only the legacy tools), then
hello{resume: true} makes the guest fold the migrated run (the log return),
so the prior conversation is in context and the run continues its seqs.

Memory projection (D23, PLAN.md 7). A guest `memory{scope: session}` event
whose `derived_from` is a compaction turn of this run (read from the fold:
`state.turns[derived_from]`) is the summary the guest folded its prefix from;
the host writes it to `{session_dir}/memory/{slug}.md` through MemoryStore
(op `write`: name "Compaction summary", description "summary of the
conversation up to seq <the turn's includes_through_seq>", type project, body
= the turn's response message content, the context item with seq ==
`response_seq`) or removes it (op `remove`), and checks the event's sha256
against `sha256_hex(body.strip())` (a mismatch is logged, never fatal: the
log, not the file, is canonical). A `memory` event derived from a call_id
(`remember`/`forget`) is only traced: the host skill wrote the file and Sides
logged the host event.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path

from protocol.canonical import sha256_hex
from protocol.events import validate_event
from protocol.fold import FoldError, State, apply

from app.config import HARNESS_SEGMENT_EVENTS
from app.session.session_dir import SessionDir, utcnow_iso
from app.user.memory import MemoryStore

logger = logging.getLogger("host.state_sync")

class StateSyncError(Exception):
    """An event the host refuses to store (schema or fold error)."""


_SEGMENT_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9].jsonl"     # a run's segments
_HOST_SEGMENT_GLOB = "host-" + _SEGMENT_GLOB                 # the host log's
_RUN_GLOB = "r[0-9][0-9][0-9][0-9]"


def _segment_name(n: int, prefix: str = "") -> str:
    return f"{prefix}{n:06d}.jsonl"


def _host_segments(events_dir: Path) -> list[Path]:
    return sorted(Path(events_dir).glob(_HOST_SEGMENT_GLOB))


def _run_dir(events_dir: Path, run: int) -> Path:
    return Path(events_dir) / f"r{run:04d}"


def _runs(events_dir: Path) -> list[int]:
    """The run numbers under `events_dir`, in order."""
    return sorted(int(p.name[1:]) for p in Path(events_dir).glob(_RUN_GLOB) if p.is_dir())


def _append_line(path: Path, obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def count_host_events(events_dir: Path, segment_events: int = HARNESS_SEGMENT_EVENTS) -> int:
    """The highest hseq in the session's host log (0 when there is none):
    full segments before the last one, plus the last one's lines."""
    host = _host_segments(events_dir)
    if not host:
        return 0
    with host[-1].open("rb") as f:
        return (len(host) - 1) * segment_events + sum(1 for _ in f)


def append_host_event(events_dir: Path, hseq: int, session_id: str | None, type: str,
                      body: dict, segment_events: int = HARNESS_SEGMENT_EVENTS
                      ) -> tuple[Path, dict]:
    """Write host event `hseq` to its segment: (the segment, the record).
    The caller is the session's one host-log writer (it owns the hseq
    counter)."""
    n = (hseq - 1) // segment_events + 1
    rec = {"ts": utcnow_iso(), "hseq": hseq, "session_id": session_id, "type": type, "body": body}
    Path(events_dir).mkdir(parents=True, exist_ok=True)
    path = Path(events_dir) / _segment_name(n, "host-")
    _append_line(path, rec)
    return path, rec


class HostLog:
    """The one writer of a session's host log in this process: hseq counted
    from disk, then incremented in memory under one lock. Get it with
    host_log(events_dir); StateSync and POST /internal/gateway-events share it.
    The count is taken from disk again whenever the segment it last wrote is
    gone or replaced (the session directory was deleted, maybe re-created
    under the same id)."""

    def __init__(self, events_dir: Path, segment_events: int = HARNESS_SEGMENT_EVENTS) -> None:
        self.events_dir = Path(events_dir)
        self.segment_events = max(1, int(segment_events))
        self._lock = threading.Lock()
        self._count: int | None = None
        self._last: tuple[Path, int] | None = None     # the segment last written, its inode

    def _stale(self) -> bool:
        if self._count is None:
            return True
        if self._last is None:
            return False
        path, ino = self._last
        try:
            return path.stat().st_ino != ino
        except OSError:
            return True

    def append(self, session_id: str | None, type: str, body: dict) -> dict:
        with self._lock:
            if self._stale():
                self._count = count_host_events(self.events_dir, self.segment_events)
            self._count += 1
            path, rec = append_host_event(self.events_dir, self._count, session_id, type, body,
                                          self.segment_events)
            self._last = (path, path.stat().st_ino)
            return rec


_host_logs: dict[Path, HostLog] = {}
_host_logs_guard = threading.Lock()


def host_log(events_dir: Path, segment_events: int = HARNESS_SEGMENT_EVENTS) -> HostLog:
    """The process-wide HostLog of `events_dir`."""
    key = Path(events_dir).resolve()
    with _host_logs_guard:
        log = _host_logs.get(key)
        if log is None:
            log = _host_logs[key] = HostLog(key, segment_events)
        return log


def _read_run(run_dir: Path, after_seq: int, segment_events: int,
              through: int | None = None) -> list[dict]:
    """The events of one run directory with seq > after_seq (stopping at
    `through`), skipping the segments that end before after_seq."""
    out: list[dict] = []
    start_segment = after_seq // segment_events + 1
    for path in sorted(run_dir.glob(_SEGMENT_GLOB)) if run_dir.is_dir() else []:
        if int(path.stem) < start_segment:
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                seq = ev.get("seq", 0)
                if seq > after_seq:
                    out.append(ev)
                if through is not None and seq >= through:
                    break
        if through is not None and out and out[-1].get("seq", 0) >= through:
            break
    out.sort(key=lambda e: e["seq"])
    return out


def read_current_run(events_dir: Path, after_seq: int = 0,
                     segment_events: int = HARNESS_SEGMENT_EVENTS) -> list[dict]:
    """The latest run's events with seq > after_seq, without loading the
    session (no StateSync, no fold): GET /sessions/{id}/events."""
    runs = _runs(events_dir)
    return (_read_run(_run_dir(events_dir, runs[-1]), after_seq, max(1, int(segment_events)))
            if runs else [])


def _truncate(path: Path, size: int) -> None:
    try:
        with path.open("r+b") as f:
            f.truncate(size)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.error("could not roll back %s to %d bytes: %s", path, size, e)


class StateSync:
    def __init__(self, session_dir: SessionDir | Path, *,
                 segment_events: int = HARNESS_SEGMENT_EVENTS) -> None:
        self.session = (session_dir if isinstance(session_dir, SessionDir)
                        else SessionDir(Path(session_dir)))
        self.events_dir = self.session.root / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.segment_events = max(1, int(segment_events))
        self._lock = threading.RLock()
        self.run = 1
        self.state = State()
        self.ticket_id: str | None = None
        self._host_log = host_log(self.events_dir, self.segment_events)
        self.seen_inputs: set[str] = set()       # across runs
        self.settled: dict[str, dict] = {}       # input_id -> settle body, across runs
        self.question_bodies: dict[str, dict] = {}  # question_id -> body, current run
        self._migrated_start = False             # current run written by migrate --events
        self._last_type: str | None = None       # type of the current run's last event
        self._load()

    # ------------------------------------------------------------ disk
    @property
    def run_dir(self) -> Path:
        return _run_dir(self.events_dir, self.run)

    def runs(self) -> list[int]:
        return _runs(self.events_dir)

    def _load(self) -> None:
        for run in self.runs():
            self.run, self.state = run, State()
            self.question_bodies = {}
            self._migrated_start, self._last_type = False, None
            for ev in self.read(0, run=run):
                try:
                    apply(self.state, ev)
                except FoldError as e:
                    raise StateSyncError(f"run {run} does not fold: {e}") from e
                self._index(ev)
            self.seen_inputs |= self.state.seen_input_ids

    def begin_run(self) -> int:
        """Open the next run (a fresh guest with its own session_start at
        seq 1). A no-op while the current run holds no events."""
        with self._lock:
            if self.state.last_seq == 0:
                return self.run
            self.run += 1
            self.state = State()
            self.question_bodies = {}
            self._migrated_start, self._last_type = False, None
            return self.run

    def read(self, after_seq: int = 0, *, run: int | None = None) -> list[dict]:
        """Events with seq > after_seq from disk, current run by default.

        For the current run the bound (the run and its last_seq) is taken
        under the lock and the files are parsed outside it, stopping at that
        seq: a line being appended meanwhile is never parsed. Every stored
        event is acked (the host acks only after the append), so this is the
        log return (M3-CONTRACTS §4) the bulk server calls from its thread."""
        through = None
        if run is None:
            with self._lock:
                run, through = self.run, self.state.last_seq
            if through <= after_seq:
                return []
        return _read_run(_run_dir(self.events_dir, run), after_seq, self.segment_events,
                         through)

    @property
    def last_seq(self) -> int:
        return self.state.last_seq

    @property
    def session_id(self) -> str | None:
        return self.state.session_id

    # ------------------------------------------------------------ accept
    def accept(self, event: dict) -> int:
        """Store one guest event; return the seq to ack (highest contiguous)."""
        with self._lock:
            seq = event.get("seq") if isinstance(event, dict) else None
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise StateSyncError("event without an int seq")
            if seq <= self.state.last_seq:
                return self.state.last_seq          # duplicate: re-ack
            if seq != self.state.last_seq + 1:
                return self.state.last_seq          # gap: wait for the resend
            errors = validate_event(event)
            if errors:
                raise StateSyncError(f"seq {seq}: {errors[0]}")
            # Append before applying: the ack that follows promises durability,
            # so a failed write (ENOSPC, EIO) must leave last_seq where it was.
            self.run_dir.mkdir(parents=True, exist_ok=True)
            path = self.run_dir / _segment_name((seq - 1) // self.segment_events + 1)
            offset = path.stat().st_size if path.exists() else 0
            _append_line(path, event)
            try:
                apply(self.state, event)
            except FoldError as e:
                _truncate(path, offset)     # keep the on-disk run foldable
                raise StateSyncError(str(e)) from e
            self._index(event)
            try:
                self._project(event)
            except Exception as e:  # projections never break the store
                logger.warning("projection failed for seq %s: %s", seq, e)
            return self.state.last_seq

    def _index(self, event: dict) -> None:
        """What must hold across runs: the input_ids seen, each task's
        settle; and the current run's question bodies."""
        etype, b = event["type"], event["body"]
        self._last_type = etype
        if etype == "session_start":
            self._migrated_start = b.get("migrated") is True
        elif etype == "input":
            self.seen_inputs.add(b["input_id"])
        elif etype == "question":
            self.question_bodies[b["question_id"]] = b
        elif etype == "settle":
            self.settled[b["input_id"]] = b

    # ------------------------------------------------------------ migrated runs
    @property
    def migrated_run(self) -> bool:
        """The current run is a migrated legacy history no guest has
        continued yet (it still ends with the migration's checkpoint)."""
        return self._migrated_start and self._last_type == "checkpoint"

    def retool_migrated(self, tools: list[dict]) -> bool:
        """Re-pin the current migrated run's session_start.tools to `tools`
        (the effective tools of the hello about to resume it; the legacy
        tools its calls name stay offered) and recompute its checkpoint, so
        the resumed guest may call every tool it offers. Rewrites the run's
        segments (a new directory swapped in) and refolds it. False when the
        run is not an uncontinued migrated one or cannot be re-pinned."""
        from app.session.migrate import retool, write_segments
        with self._lock:
            if not self.migrated_run:
                return False
            events = _read_run(self.run_dir, 0, self.segment_events)
            try:
                events = retool(events, tools)
            except ValueError as e:
                logger.warning("migrated run %s keeps its tools: %s", self.run_dir, e)
                return False
            fresh = self.events_dir / f".{self.run_dir.name}.retool"
            old = self.events_dir / f".{self.run_dir.name}.old"
            for stale in (fresh, old):
                if stale.exists():
                    shutil.rmtree(stale)
            write_segments(fresh, events, self.segment_events)
            os.rename(self.run_dir, old)
            os.rename(fresh, self.run_dir)
            shutil.rmtree(old, ignore_errors=True)
            self.state, self.question_bodies = State(), {}
            for ev in events:
                apply(self.state, ev)
                self._index(ev)
            return True

    # ------------------------------------------------------------ host log
    def host_event(self, type: str, body: dict) -> dict:
        """Append a host-session event (workflow, side_call, vm, usage, ...)."""
        return self._host_log.append(self.state.session_id, type, body)

    def read_host(self) -> list[dict]:
        out: list[dict] = []
        for path in _host_segments(self.events_dir):
            with path.open("r", encoding="utf-8") as f:
                out.extend(json.loads(l) for l in f if l.strip())
        return out

    # ------------------------------------------------------------ projections
    def _project(self, event: dict) -> None:
        if event["type"] == "memory":
            self._project_memory(event["body"])

    # ------------------------------------------------------------ memory (D23)
    def _project_memory(self, body: dict) -> None:
        """Write or remove the session-tier file of a compaction summary."""
        derived_from = body["derived_from"]
        if body["scope"] != "session" or derived_from in self.state.open_calls:
            return                              # user tier / tool-derived: the skill wrote it
        turn = self.state.turns.get(derived_from)
        summary = self._response_message(turn) if turn and turn["type"] == "compaction" else None
        if summary is None:
            logger.warning("memory %s/%s derived_from %r is neither a tool call nor a "
                           "compaction turn of this run; not projected",
                           body["op"], body["slug"], derived_from)
            return
        store = MemoryStore(self.session.memory)
        if body["op"] == "remove":
            store.remove(body["slug"])
            return
        content = summary.get("content")
        content = content if isinstance(content, str) else ""
        store.write(slug=body["slug"], name="Compaction summary",
                    description=f"summary of the conversation up to seq "
                                f"{turn['includes_through_seq']}",
                    type="project", body=content)
        digest = sha256_hex(content.strip())
        if body["sha256"] != digest:
            logger.warning("memory %s: sha256 %s in the event differs from the summary text's %s",
                           body["slug"], body["sha256"], digest)

    def _response_message(self, turn: dict) -> dict | None:
        """The turn's llm_response message, from the folded context (the
        committed prefix, else the staged suffix); None before it is stored."""
        seq = turn["response_seq"]
        if seq is None:
            return None
        for it in (*self.state.committed, *self.state.staged_suffix):
            if it["seq"] == seq:
                return it["message"]
        return None
