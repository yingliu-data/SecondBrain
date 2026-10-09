"""state_sync — the canonical store of a session's events (PLAN.md 1, 4.2, 5).

  StateSync(session_dir)
    .accept(event) -> ack_seq     validate, append to events/NNNNNN.jsonl, fsync,
                                  fold into .state, write the projections, THEN
                                  return the seq to ack (durable = host ack after
                                  append). Duplicates (seq <= last_seq) and gaps
                                  (seq > last_seq + 1) are not appended; the ack
                                  is the highest contiguous seq either way.
    .host_event(type, body)       the worker's own log: events/host-NNNNNN.jsonl
    .read(after_seq=0)            events from disk, in seq order (safe from another
                                  thread: the bulk server's log return)
    .state                        protocol.fold.State folded from the log
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

Projections (kept one release, PLAN.md 3): `history.jsonl` gets the user text
of each new task/follow_up/steer input and the assistant answer at settle
(SessionDir.append_history); `logs/trace.jsonl` gets today's event names
(SessionDir.append_trace): tool_call, tool_result, assistant_response,
llm_error, loop_exhausted — plus question, settle, error and memory for the
viewer.

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
import threading
from pathlib import Path

from protocol.canonical import sha256_hex
from protocol.events import validate_event
from protocol.fold import FoldError, State, apply

from app.config import HARNESS_SEGMENT_EVENTS
from app.session.session_dir import SessionDir, utcnow_iso
from app.user.memory import MemoryStore

logger = logging.getLogger("host.state_sync")

_ERROR_TRACE = {
    "transient": "llm_error", "overflow": "llm_error", "budget": "llm_error",
    "limit": "loop_exhausted", "protocol": "error", "fatal": "error",
}


class StateSyncError(Exception):
    """An event the host refuses to store (schema or fold error)."""


def _segment_name(n: int, prefix: str = "") -> str:
    return f"{prefix}{n:06d}.jsonl"


def _append_line(path: Path, obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def count_host_events(events_dir: Path, segment_events: int = HARNESS_SEGMENT_EVENTS) -> int:
    """The highest hseq in the session's host log (0 when there is none):
    full segments before the last one, plus the last one's lines."""
    host = sorted(p for p in Path(events_dir).glob("host-[0-9][0-9][0-9][0-9][0-9][0-9].jsonl"))
    if not host:
        return 0
    with host[-1].open("rb") as f:
        return (len(host) - 1) * segment_events + sum(1 for _ in f)


def append_host_event(events_dir: Path, hseq: int, session_id: str | None, type: str,
                      body: dict, segment_events: int = HARNESS_SEGMENT_EVENTS) -> dict:
    """Write host event `hseq` to its segment. The caller is the session's
    one host-log writer (it owns the hseq counter)."""
    n = (hseq - 1) // segment_events + 1
    rec = {"ts": utcnow_iso(), "hseq": hseq, "session_id": session_id, "type": type, "body": body}
    Path(events_dir).mkdir(parents=True, exist_ok=True)
    _append_line(Path(events_dir) / _segment_name(n, "host-"), rec)
    return rec


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
        self._host_count = 0
        self.seen_inputs: set[str] = set()       # across runs
        self.settled: dict[str, dict] = {}       # input_id -> settle body, across runs
        self._load()

    # ------------------------------------------------------------ disk
    @property
    def run_dir(self) -> Path:
        return self.events_dir / f"r{self.run:04d}"

    def runs(self) -> list[int]:
        out = []
        for p in self.events_dir.glob("r[0-9][0-9][0-9][0-9]"):
            if p.is_dir():
                out.append(int(p.name[1:]))
        return sorted(out)

    def _segments(self, prefix: str = "", run: int | None = None) -> list[Path]:
        pat = f"{prefix}[0-9][0-9][0-9][0-9][0-9][0-9].jsonl"
        base = self.events_dir if prefix else self.events_dir / f"r{run or self.run:04d}"
        if not base.is_dir():
            return []
        return sorted(p for p in base.glob(pat) if p.name.startswith(prefix))

    def _load(self) -> None:
        runs = self.runs()
        for run in runs:
            state = State()
            for ev in self.read(0, run=run):
                try:
                    apply(state, ev)
                except FoldError as e:
                    raise StateSyncError(f"run {run} does not fold: {e}") from e
                if ev["type"] == "settle":
                    self.settled[ev["body"]["input_id"]] = ev["body"]
            self.seen_inputs |= state.seen_input_ids
            self.run, self.state = run, state
        self._host_count = count_host_events(self.events_dir, self.segment_events)

    def begin_run(self) -> int:
        """Open the next run (a fresh guest with its own session_start at
        seq 1). A no-op while the current run holds no events."""
        with self._lock:
            if self.state.last_seq == 0:
                return self.run
            self.run += 1
            self.state = State()
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
        out: list[dict] = []
        start_segment = after_seq // self.segment_events + 1
        for path in self._segments(run=run):
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
        """What must hold across runs: the input_ids seen, each task's settle."""
        etype, b = event["type"], event["body"]
        if etype == "input":
            self.seen_inputs.add(b["input_id"])
        elif etype == "settle":
            self.settled[b["input_id"]] = b

    # ------------------------------------------------------------ host log
    def host_event(self, type: str, body: dict) -> dict:
        """Append a host-session event (workflow, side_call, vm, usage, ...)."""
        with self._lock:
            self._host_count += 1
            return append_host_event(self.events_dir, self._host_count, self.state.session_id,
                                     type, body, self.segment_events)

    def read_host(self) -> list[dict]:
        out: list[dict] = []
        for path in self._segments("host-"):
            with path.open("r", encoding="utf-8") as f:
                out.extend(json.loads(l) for l in f if l.strip())
        return out

    # ------------------------------------------------------------ projections
    def _trace(self, event: str, detail: dict | None = None) -> None:
        self.session.append_trace(event, detail, ticket_id=self.ticket_id)

    def _project(self, event: dict) -> None:
        etype, body = event["type"], event["body"]
        if etype == "input":
            if body["dedupe"] == "new" and body["kind"] in ("task", "follow_up", "steer"):
                self.session.append_history("user", body["text"], ticket_id=self.ticket_id)
        elif etype == "tool_call":
            self._trace("tool_call", {"name": body["name"], "arguments": body["args"],
                                      "call_id": body["call_id"], "side": body["side"]})
        elif etype == "tool_result":
            call = self.state.open_calls.get(body["call_id"], {})
            detail = {"name": call.get("name", "?"), "result": body["content"][:2000],
                      "call_id": body["call_id"]}
            if "duration_ms" in call:
                detail["duration_ms"] = call["duration_ms"]
            self._trace("tool_result", detail)
        elif etype == "llm_response":
            msg = body["message"]
            if body["finish_reason"] != "tool_calls" and not msg.get("tool_calls"):
                text = (msg.get("content") or "").strip()
                self._trace("assistant_response",
                            {"chars": len(text), "turn_id": body["turn_id"]})
        elif etype == "error":
            self._trace(_ERROR_TRACE.get(body["class"], "error"),
                        {"error": body["message"][:500], "class": body["class"]})
        elif etype == "question":
            self._trace("question", {"question_id": body["question_id"],
                                     "text": body["text"][:500]})
        elif etype == "settle":
            if body["answer"]:
                self.session.append_history("assistant", body["answer"],
                                            ticket_id=self.ticket_id)
            self._trace("settle", {"input_id": body["input_id"], "outcome": body["outcome"],
                                   "usage": body["usage"]})
        elif etype == "memory":
            self._trace("memory", {"scope": body["scope"], "op": body["op"], "slug": body["slug"],
                                   "sha256": body["sha256"], "derived_from": body["derived_from"]})
            self._project_memory(body)

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
