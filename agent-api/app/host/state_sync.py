"""state_sync — the canonical store of a session's events.

  StateSync(session_dir)
    .submit(event) -> Future|None validate, fold into .state, queue the append to
                                  events/rNNNN/NNNNNN.jsonl on the process's group
                                  writer (durable.py); the future resolves once
                                  fsynced, and only then is the event acked
                                  (.last_seq moves). None: a duplicate (seq <=
                                  the folded seq) or a gap (seq > it + 1):
                                  nothing stored. The worker's pump submits a
                                  burst of events, awaits the last future, acks
                                  once (one write + one fsync for the burst).
    .accept(event) -> ack_seq     submit + wait (blocking; tests, tools) + the
                                  projection: the seq to ack
    .project(event)               the memory projection (after durable)
    .host_event(type, body)       the worker's own log: events/host-NNNNNN.jsonl
                                  (through host_log(events_dir): one writer per
                                  directory in the process; group-committed,
                                  never waited for on the loop)
    .read(after_seq=0)            acked events from disk, in seq order (safe from
                                  another thread: the bulk server's log return)
  read_current_run(events_dir, after_seq)   the same without loading the session
  write_segments(run_dir, events) / replace_run_dir(run_dir, events)
                                the segment writer (migration and the retool
                                below): a new run directory, or one swapped in
  retool(events, tools)         a migrated run re-pinned to a hello's tools
    .state                        protocol.fold.State folded from the log (its
                                  index fields: call_args, question_text,
                                  settles_by_input, open_input)
    .settled                      input_id -> settle body, across runs
    .seen_inputs                  every input_id seen, across runs
    .open_input                   the input_id of the current run's task that has
                                  no settle yet (None: the run is between tasks)
    .last_seq                     highest acked (durable) seq

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
settle of each task - comes from the current run's fold plus a cheap scan
of the earlier runs at load (protocol.replay.SessionIndex: only their input
and settle lines are parsed; only the last run is folded), so a redelivered
input is answered without a guest. The host
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

import concurrent.futures
import functools
import logging
import os
import shutil
import threading
from collections import ChainMap
from pathlib import Path

from protocol.canonical import is_int, sha256_hex
from protocol.clock import now_iso
from protocol.events import validate_event
from protocol.fold import FoldError, State, apply, apply_valid, checkpoint_body, fold, replay
from protocol.fsio import swap_dir
from protocol.limits import SEGMENT_EVENTS
from protocol.messages import encode
from protocol.replay import (SessionIndex, host_segments, iter_segment, run_name, run_numbers,
                             segment_name, segments, session_index)

from app.host.durable import GroupWriter, writer as default_writer
from app.session.session_dir import SessionDir
from app.user.memory import MemoryStore

logger = logging.getLogger("host.state_sync")


class StateSyncError(Exception):
    """An event the host refuses to store (schema or fold error)."""


def _run_dir(events_dir: Path, run: int) -> Path:
    return Path(events_dir) / run_name(run)


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
    if fresh.exists():
        shutil.rmtree(fresh)
    write_segments(fresh, events, segment_events)
    swap_dir(fresh, run_dir)


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


def host_record(hseq: int, session_id: str | None, type: str, body: dict) -> dict:
    return {"ts": now_iso(), "hseq": hseq, "session_id": session_id, "type": type, "body": body}


def _log_failure(fut: concurrent.futures.Future) -> None:
    if fut.exception() is not None:
        logger.error("host event not stored: %s", fut.exception())


class HostLog:
    """The one writer of a session's host log in this process: hseq counted
    from disk, then incremented in memory under one lock, each record
    handed to the group writer in hseq order (durable.py: file order = hseq
    order). Get it with host_log(events_dir); StateSync and POST
    /internal/gateway-events share it. The count is taken from disk again
    whenever the segment it last stored is gone or replaced (the session
    directory was deleted, maybe re-created under the same id); records
    still queued are flushed first.

      .submit(session_id, type, body) -> (record, future)
      .append(...) -> record          fire and forget (a failure is logged)
      .append_durable(...) -> record  waits until it is fsynced (off the loop)
      .flush(timeout=None)            wait for every record submitted so far"""

    def __init__(self, events_dir: Path, segment_events: int = SEGMENT_EVENTS,
                 writer: GroupWriter | None = None) -> None:
        self.events_dir = Path(events_dir)
        self.segment_events = max(1, int(segment_events))
        self._writer = writer or default_writer()
        self._lock = threading.Lock()
        self._count = 0
        self._known: tuple[Path, int] | None = None   # the segment last stored, its inode
        self._last: concurrent.futures.Future | None = None

    def _stale(self) -> bool:
        if self._known is None:
            return self._last is None       # nothing submitted yet: count from disk
        path, ino = self._known
        try:
            return path.stat().st_ino != ino
        except OSError:
            return True

    def _stored(self, path: Path, fut: concurrent.futures.Future) -> None:
        if not fut.cancelled() and fut.exception() is None:
            self._known = (path, fut.result())

    def submit(self, session_id: str | None, type: str, body: dict
               ) -> tuple[dict, concurrent.futures.Future]:
        with self._lock:
            if self._stale():
                if self._last is not None:
                    concurrent.futures.wait([self._last])
                self._known = None
                self.events_dir.mkdir(parents=True, exist_ok=True)
                self._count = count_host_events(self.events_dir, self.segment_events)
            self._count += 1
            rec = host_record(self._count, session_id, type, body)
            n = (self._count - 1) // self.segment_events + 1
            path = self.events_dir / segment_name(n, host=True)
            self._last = fut = self._writer.append(path, encode(rec).encode("utf-8"))
            fut.add_done_callback(functools.partial(self._stored, path))
            return rec, fut

    def append(self, session_id: str | None, type: str, body: dict) -> dict:
        rec, fut = self.submit(session_id, type, body)
        fut.add_done_callback(_log_failure)
        return rec

    def append_durable(self, session_id: str | None, type: str, body: dict) -> dict:
        rec, fut = self.submit(session_id, type, body)
        fut.result()
        return rec

    def flush(self, timeout: float | None = None) -> None:
        last = self._last
        if last is not None:
            concurrent.futures.wait([last], timeout=timeout)


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


class StateSync:
    def __init__(self, session_dir: SessionDir | Path, *,
                 segment_events: int = SEGMENT_EVENTS,
                 writer: GroupWriter | None = None) -> None:
        self.session = (session_dir if isinstance(session_dir, SessionDir)
                        else SessionDir(Path(session_dir)))
        self.events_dir = self.session.events
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.segment_events = max(1, int(segment_events))
        self._writer = writer or default_writer()
        self._lock = threading.RLock()
        self.run = 1
        self.state = State()
        self._acked = 0                          # highest durable seq of the current run
        self._made: Path | None = None           # the run directory known to exist
        self._host_log = host_log(self.events_dir, self.segment_events)
        self._prior = SessionIndex()             # the runs before the current one
        self._load()

    # ------------------------------------------------------------ disk
    @property
    def run_dir(self) -> Path:
        return _run_dir(self.events_dir, self.run)

    def runs(self) -> list[int]:
        return run_numbers(self.events_dir)

    def _load(self) -> None:
        """The last run folded in full; the earlier ones only scanned for
        their inputs and settles (SessionIndex: nothing folded)."""
        runs = self.runs()
        self._prior = session_index(self.events_dir, runs[:-1])
        self.state, self._acked, self._made = State(), 0, None
        if not runs:
            return
        self.run = runs[-1]
        try:
            self._refold()
        except FoldError as e:
            raise StateSyncError(f"run {self.run} does not fold: {e}") from e

    def _refold(self) -> None:
        """The current run folded anew from disk (a FoldError propagates)."""
        self.state, self._acked, self._made = State(), 0, None
        for ev in _read_run(self.run_dir, 0, self.segment_events):
            apply(self.state, ev)
        self._acked = self.state.last_seq

    def begin_run(self) -> int:
        """Open the next run (a fresh guest with its own session_start at
        seq 1). A no-op while the current run holds no events."""
        with self._lock:
            if self.state.last_seq == 0:
                return self.run
            self._prior.add_state(self.state)
            self.run += 1
            self.state, self._acked, self._made = State(), 0, None
            return self.run

    def read(self, after_seq: int = 0, *, run: int | None = None) -> list[dict]:
        """Acked events with seq > after_seq from disk, current run by default.

        For the current run the bound (the run and its acked seq) is taken
        under the lock and the files are parsed outside it, stopping at that
        seq: a line being appended meanwhile is never parsed. Only acked
        (durable) events are returned, so this is the log return the bulk
        server calls from its thread."""
        through = None
        if run is None:
            with self._lock:
                run, through = self.run, self._acked
            if through <= after_seq:
                return []
        return _read_run(_run_dir(self.events_dir, run), after_seq, self.segment_events,
                         through)

    @property
    def last_seq(self) -> int:
        """The highest acked seq: stored and fsynced (the ack the host sends)."""
        return self._acked

    @property
    def session_id(self) -> str | None:
        return self.state.session_id

    # ------------------------------------------------------------ across runs
    @property
    def settled(self) -> ChainMap:
        """input_id -> its settle body, across runs (the current run first)."""
        return ChainMap(self.state.settles_by_input, self._prior.settles)

    @property
    def seen_inputs(self) -> set[str]:
        """Every input_id seen, across runs."""
        return self._prior.seen_input_ids | self.state.seen_input_ids

    @property
    def open_input(self) -> str | None:
        """The current run's task that has no settle yet."""
        return self.state.open_input

    # ------------------------------------------------------------ accept
    def submit(self, event: dict) -> concurrent.futures.Future | None:
        """Validate and fold one guest event and queue its durable append;
        the future resolves once it is fsynced, and the acked seq moves then.
        None for a duplicate or a gap (nothing stored). Raises
        StateSyncError for an event the host refuses (schema or fold error:
        nothing is written) and for a log whose earlier write failed (the
        state is then refolded from disk first, so the guest's resend of
        the lost events is accepted again)."""
        with self._lock:
            seq = event.get("seq") if isinstance(event, dict) else None
            if not is_int(seq):
                raise StateSyncError("event without an int seq")
            failed = self._writer.poisoned(self)
            if failed is not None:
                self._recover()
                raise StateSyncError(f"the event log could not be written: {failed}")
            if seq != self.state.last_seq + 1:
                return None                         # duplicate (re-ack) or gap (wait)
            errors = validate_event(event)
            if errors:
                raise StateSyncError(f"seq {seq}: {errors[0]}")
            try:
                apply_valid(self.state, event)
            except FoldError as e:
                raise StateSyncError(str(e)) from e
            run_dir = self.run_dir
            if self._made != run_dir:
                run_dir.mkdir(parents=True, exist_ok=True)
                self._made = run_dir
            path = run_dir / segment_name((seq - 1) // self.segment_events + 1)
            fut = self._writer.append(path, encode(event).encode("utf-8"), group=self)
            fut.sb_stored = (self.run, seq)
            fut.add_done_callback(self.stored)
            return fut

    def stored(self, fut: concurrent.futures.Future) -> None:
        """A submitted append resolved (the writer's thread, or the caller
        that waited on it: idempotent): its seq is acked when it is stored."""
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            run, seq = fut.sb_stored
            with self._lock:
                if run == self.run and seq > self._acked:
                    self._acked = seq

    def _recover(self) -> None:
        """A write failed: refold the current run from disk (the writer cut
        the failed batch back), clear the failure. The events folded but
        never stored are gone from the state; the guest resends them."""
        self._writer.reset(self)
        run = self.run
        self._refold()
        logger.error("session %s run %s: event log write failed; refolded to seq %s",
                     self.session.root.name, run, self._acked)

    def accept(self, event: dict) -> int:
        """Store one guest event (submit, then block until it is durable,
        then project it); the seq to ack (the highest acked). Blocking: the
        worker's pump uses submit() and awaits the future instead."""
        fut = self.submit(event)
        if fut is not None:
            try:
                fut.result()
            except OSError as e:
                raise StateSyncError(f"seq {event['seq']}: not stored: {e}") from e
            self.stored(fut)        # result() may return before the callback ran
            self.project(event)
        return self.last_seq

    def project(self, event: dict) -> None:
        """The projections of a stored event (memory); never raises."""
        try:
            self._project(event)
        except Exception as e:  # projections never break the store
            logger.warning("projection failed for seq %s: %s", event.get("seq"), e)

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
            self.state = fold(events)
            self._acked = self.state.last_seq
            return True

    # ------------------------------------------------------------ host log
    def host_event(self, type: str, body: dict) -> dict:
        """Append a host-session event (workflow, side_call, vm, usage, ...):
        group-committed, not waited for (telemetry); the record."""
        return self._host_log.append(self.state.session_id, type, body)

    def flush_host(self, timeout: float | None = None) -> None:
        """Wait until every host event appended so far is on disk."""
        self._host_log.flush(timeout)

    def read_host(self) -> list[dict]:
        """The host log from disk (records still queued are flushed first)."""
        self.flush_host()
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
