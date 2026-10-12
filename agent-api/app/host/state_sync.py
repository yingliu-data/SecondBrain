"""state_sync — the canonical store of a session's events.

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
  write_segments(run_dir, events) / replace_run_dir(run_dir, events)
                                the segment writer (migration and the retool
                                below): a new run directory, or one swapped in
  retool(events, tools)         a migrated run re-pinned to a hello's tools
    .state                        protocol.fold.State folded from the log
    .question_bodies              question_id -> the `question` body (current run)
    .open_input                   the input_id of the current run's task that has
                                  no settle yet (None: the run is between tasks)
    .last_seq                     highest stored seq

Segments hold protocol.limits.SEGMENT_EVENTS events each (seq 1..N in 000001.jsonl,
N+1..2N in 000002.jsonl), so the segment of a seq is a function of the seq.
Segment files are read with protocol.replay.iter_segment: a line that is not
JSON raises (ReplayError, a ValueError) - the store never skips an event.

Runs. A fresh guest (hello{resume: false}) begins with its own
`session_start` at seq 1, so it opens a new run; a resumed guest (a parked
snapshot restored, or a death resume with the log return) continues the
current run's seq. Each run is therefore its own log under
`events/rNNNN/` (segments inside) with its own folded State; `begin_run()`
opens the next one. What must hold across runs - the input_ids seen and the
settle of each task - is folded from every run at load (`seen_inputs`,
`settled`), so a redelivered input is answered without a guest. The host
log `events/host-NNNNNN.jsonl` is one per session, across runs. With one
long-lived guest per session there is one run, so the layout is a single
`events/000001.jsonl` log one directory down.

Projections. The event log is the only record of the conversation and the
trace: the `history.jsonl` / `logs/trace.jsonl` projections are retired
(readers fold the log: app.session.eventlog). What remains is the memory
projection below.

Migrated runs. `migrated_run` is True while the
current run is one `app.session.migrate` wrote from a legacy history (its
session_start has `migrated: true`) and no guest has continued it yet (it
still ends with the migration's checkpoint); it is read from disk when
asked (at a fresh boot). The worker's first boot of such a session is a
resume: `retool_migrated(tools)` re-pins the run's session_start.tools to
the hello's effective tools (the legacy run never had a guest; its own
allowlist holds only the legacy tools), then hello{resume: true} makes the
guest fold the migrated run (the log return), so the prior conversation is
in context and the run continues its seqs.

Memory projection. A guest `memory{scope: session}` event
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

import logging
import os
import shutil
import threading
from pathlib import Path

from protocol.canonical import sha256_hex
from protocol.clock import now_iso
from protocol.events import validate_event
from protocol.fold import FoldError, State, apply, checkpoint_body, replay
from protocol.limits import SEGMENT_EVENTS
from protocol.messages import encode
from protocol.replay import (host_segments, iter_segment, run_name, run_numbers, segment_name,
                             segments)

from app.session.session_dir import SessionDir
from app.user.memory import MemoryStore

logger = logging.getLogger("host.state_sync")

# the input kinds that start a task when none runs (guest.coordinator.INPUT_KINDS)
TASK_INPUT_KINDS = ("task", "follow_up", "steer")

class StateSyncError(Exception):
    """An event the host refuses to store (schema or fold error)."""


def _run_dir(events_dir: Path, run: int) -> Path:
    return Path(events_dir) / run_name(run)


def _append_line(path: Path, obj: dict) -> int:
    """Append `obj` as one durable line; the file's inode."""
    with path.open("a", encoding="utf-8") as f:
        f.write(encode(obj))
        f.flush()
        os.fsync(f.fileno())
        return os.fstat(f.fileno()).st_ino


def write_segments(run_dir: Path, events: list[dict],
                   segment_events: int = SEGMENT_EVENTS) -> None:
    """A run's events as segment files (seq 1..N in 000001.jsonl, ...) in a
    new directory `run_dir`."""
    segment_events = max(1, int(segment_events))
    run_dir.mkdir(parents=True)
    for start in range(0, len(events), segment_events):
        with (run_dir / segment_name(start // segment_events + 1)).open("w", encoding="utf-8") as f:
            f.writelines(encode(ev) for ev in events[start:start + segment_events])
            f.flush()
            os.fsync(f.fileno())


def replace_run_dir(run_dir: Path, events: list[dict],
                    segment_events: int = SEGMENT_EVENTS) -> None:
    """`run_dir` (absent or not) holding exactly `events`: written to a
    temporary sibling, swapped in by rename, the old directory removed; a
    reader sees the old run or the new one, never a part."""
    run_dir = Path(run_dir)
    fresh = run_dir.with_name(f".{run_dir.name}.new")
    old = run_dir.with_name(f".{run_dir.name}.old")
    for stale in (fresh, old):
        if stale.exists():
            shutil.rmtree(stale)
    write_segments(fresh, events, segment_events)
    if run_dir.exists():
        os.rename(run_dir, old)
    os.rename(fresh, run_dir)
    shutil.rmtree(old, ignore_errors=True)


def retool(events: list[dict], tools: list[dict]) -> list[dict]:
    """A migrated run, not yet continued, re-pinned to the tool list of the
    hello that is about to resume it: session_start.tools = `tools` plus the
    legacy tools the run's tool_calls name (so they stay allowed), and the
    closing checkpoint recomputed. The legacy run never had a guest, so its
    allowlist is whatever the first harness guest offers. Raises ValueError
    when the run is not a migrated, uncontinued one, or when a legacy tool
    shares its name with an offered tool on another side."""
    if not events or events[0]["type"] != "session_start" \
            or events[0]["body"].get("migrated") is not True:
        raise ValueError("not a migrated run")
    if events[-1]["type"] != "checkpoint":
        raise ValueError("the migrated run was continued already")
    offered = {t["name"]: t for t in tools}
    merged = [dict(t) for t in tools]
    for t in events[0]["body"]["tools"]:
        have = offered.get(t["name"])
        if have is None:
            merged.append(dict(t))
            offered[t["name"]] = t
        elif have["side"] != t["side"]:
            raise ValueError(f"legacy tool {t['name']!r} (side {t['side']}) is offered "
                             f"on side {have['side']}")
    body = [{**events[0], "body": {**events[0]["body"],
                                   "tools": sorted(merged, key=lambda t: t["name"])}},
            *events[1:-1]]
    state, problems = replay(body)
    if problems:
        raise ValueError(f"retooled run does not verify: {problems[0]}")
    return [*body, {**events[-1], "body": checkpoint_body(body, state=state)}]


def count_host_events(events_dir: Path, segment_events: int = SEGMENT_EVENTS) -> int:
    """The highest hseq in the session's host log (0 when there is none):
    full segments before the last one, plus the last one's lines."""
    host = host_segments(events_dir)
    if not host:
        return 0
    with host[-1].open("rb") as f:
        return (len(host) - 1) * segment_events + sum(1 for _ in f)


def append_host_event(events_dir: Path, hseq: int, session_id: str | None, type: str,
                      body: dict, segment_events: int = SEGMENT_EVENTS
                      ) -> tuple[Path, int, dict]:
    """Write host event `hseq` to its segment (events_dir exists): (the
    segment, its inode, the record). The caller is the session's one
    host-log writer (it owns the hseq counter)."""
    n = (hseq - 1) // segment_events + 1
    rec = {"ts": now_iso(), "hseq": hseq, "session_id": session_id, "type": type, "body": body}
    path = Path(events_dir) / segment_name(n, host=True)
    return path, _append_line(path, rec), rec


class HostLog:
    """The one writer of a session's host log in this process: hseq counted
    from disk, then incremented in memory under one lock. Get it with
    host_log(events_dir); StateSync and POST /internal/gateway-events share it.
    The count is taken from disk again whenever the segment it last wrote is
    gone or replaced (the session directory was deleted, maybe re-created
    under the same id)."""

    def __init__(self, events_dir: Path, segment_events: int = SEGMENT_EVENTS) -> None:
        self.events_dir = Path(events_dir)
        self.segment_events = max(1, int(segment_events))
        self._lock = threading.Lock()
        self._count = 0
        self._last: tuple[Path, int] | None = None     # the segment last written, its inode

    def _stale(self) -> bool:
        if self._last is None:
            return True
        path, ino = self._last
        try:
            return path.stat().st_ino != ino
        except OSError:
            return True

    def append(self, session_id: str | None, type: str, body: dict) -> dict:
        with self._lock:
            if self._stale():
                self.events_dir.mkdir(parents=True, exist_ok=True)
                self._count = count_host_events(self.events_dir, self.segment_events)
            self._count += 1
            path, ino, rec = append_host_event(self.events_dir, self._count, session_id, type,
                                               body, self.segment_events)
            self._last = (path, ino)
            return rec


_host_logs: dict[Path, HostLog] = {}
_host_logs_guard = threading.Lock()


def host_log(events_dir: Path, segment_events: int = SEGMENT_EVENTS) -> HostLog:
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
    for path in segments(run_dir):
        if int(path.stem) < start_segment:
            continue
        for ev in iter_segment(path):
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
                     segment_events: int = SEGMENT_EVENTS) -> list[dict]:
    """The latest run's events with seq > after_seq, without loading the
    session (no StateSync, no fold): GET /sessions/{id}/events."""
    runs = run_numbers(events_dir)
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
                 segment_events: int = SEGMENT_EVENTS) -> None:
        self.session = (session_dir if isinstance(session_dir, SessionDir)
                        else SessionDir(Path(session_dir)))
        self.events_dir = self.session.events
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.segment_events = max(1, int(segment_events))
        self._lock = threading.RLock()
        self.run = 1
        self.state = State()
        self._host_log = host_log(self.events_dir, self.segment_events)
        self.seen_inputs: set[str] = set()       # across runs
        self.settled: dict[str, dict] = {}       # input_id -> settle body, across runs
        self.question_bodies: dict[str, dict] = {}  # question_id -> body, current run
        self.open_input: str | None = None          # the current run's unsettled task
        self._load()

    # ------------------------------------------------------------ disk
    @property
    def run_dir(self) -> Path:
        return _run_dir(self.events_dir, self.run)

    def runs(self) -> list[int]:
        return run_numbers(self.events_dir)

    def _load(self) -> None:
        for run in self.runs():
            self.run, self.state = run, State()
            self.question_bodies, self.open_input = {}, None
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
            self.question_bodies, self.open_input = {}, None
            return self.run

    def read(self, after_seq: int = 0, *, run: int | None = None) -> list[dict]:
        """Events with seq > after_seq from disk, current run by default.

        For the current run the bound (the run and its last_seq) is taken
        under the lock and the files are parsed outside it, stopping at that
        seq: a line being appended meanwhile is never parsed. Every stored
        event is acked (the host acks only after the append), so this is the
        log return the bulk server calls from its thread."""
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
            path = self.run_dir / segment_name((seq - 1) // self.segment_events + 1)
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
        settle; and the current run's question bodies and unsettled task
        (the guest's: the first new task/follow_up/steer input after a
        settle starts a task, its settle ends it)."""
        etype, b = event["type"], event["body"]
        if etype == "input":
            self.seen_inputs.add(b["input_id"])
            if self.open_input is None and b.get("dedupe") == "new" \
                    and b.get("kind") in TASK_INPUT_KINDS:
                self.open_input = b["input_id"]
        elif etype == "question":
            self.question_bodies[b["question_id"]] = b
        elif etype == "settle":
            self.settled[b["input_id"]] = b
            self.open_input = None

    # ------------------------------------------------------------ migrated runs
    def _migrated_events(self) -> list[dict] | None:
        """The current run's events when it is a migrated legacy history no
        guest has continued yet (it still ends with the migration's
        checkpoint), else None. Reads the first line, the run only then."""
        if self.state.last_seq == 0:
            return None
        head = next(iter_segment(segments(self.run_dir)[0]))
        if head["body"].get("migrated") is not True:
            return None
        events = self.read(0)
        return events if events[-1]["type"] == "checkpoint" else None

    @property
    def migrated_run(self) -> bool:
        """The current run is a migrated legacy history no guest has
        continued yet."""
        with self._lock:
            return self._migrated_events() is not None

    def retool_migrated(self, tools: list[dict]) -> bool:
        """Re-pin the current migrated run's session_start.tools to `tools`
        (the effective tools of the hello about to resume it; the legacy
        tools its calls name stay offered) and recompute its checkpoint, so
        the resumed guest may call every tool it offers. Rewrites the run's
        segments (replace_run_dir) and refolds it. False when the run is not
        an uncontinued migrated one or cannot be re-pinned."""
        with self._lock:
            events = self._migrated_events()
            if events is None:
                return False
            try:
                events = retool(events, tools)
            except ValueError as e:
                logger.warning("migrated run %s keeps its tools: %s", self.run_dir, e)
                return False
            replace_run_dir(self.run_dir, events, self.segment_events)
            self.state, self.question_bodies, self.open_input = State(), {}, None
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
        for path in host_segments(self.events_dir):
            out.extend(iter_segment(path))
        return out

    # ------------------------------------------------------------ projections
    def _project(self, event: dict) -> None:
        if event["type"] == "memory":
            self._project_memory(event["body"])

    # ------------------------------------------------------------ memory
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
