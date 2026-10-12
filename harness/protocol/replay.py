"""replay — load, fold and verify a session's event logs from disk
(docs/REPLAY.md).

    python -m protocol.replay replay <target>...         fold, print a state summary + state_hash
    python -m protocol.replay verify <target>...         validate + fold + checkpoint chain
    python -m protocol.replay verify --all <root>...     every session dir under each root
    (--json on either for machine-readable output)

A target is a session directory, its `events/` directory, or one `.jsonl`
file. The layout is read from the files alone (no agent-api import):

    <session>/events/r0001/000001.jsonl, 000002.jsonl, ...   one run per guest
    <session>/events/r0002/...                               (a fresh guest: seq 1 again)
    <session>/events/host-000001.jsonl, ...                  the host log, one per session
    <log_dir>/events/000001.jsonl, ...                       a flat log (the guest's local
                                                             buffer): one run

Public functions (agent-api reuses them):
  load_session(path) -> list[list[event]]    the runs in order, each in seq order;
                                             raises ReplayError when a file is unreadable
  iter_runs(path, newest_first=, modified_since=)   the same, lazily, the host log
                                             unread; runs last written before
                                             modified_since (epoch s) left out
  verify_session(path) -> list[str]          problems, [] == clean. Each names the seq:
                                             "<run>: seq N: reason" (run = r0001, ..., or
                                             "events" for a flat log, the file name for a
                                             .jsonl target), "host: hseq N: reason" for the
                                             host log, "<path>: reason" for the layout
  find_sessions(root, include_before_resume=)   every directory under root holding an
                                             event log (+ the guest's events.before-resume-N
                                             buffers)
  summarize(state) -> dict                   the replay summary of a folded State
  SessionIndex                               what holds across runs: seen_input_ids,
                                             settles (input_id -> settle body);
                                             .add_events(events) .add_state(state)
                                             .scan_run(run_dir) .settled(input_id)
  session_index(path, runs=None)             a SessionIndex of the session's runs (all,
                                             or the run numbers given), scanned cheaply:
                                             only `input` and `settle` lines are parsed,
                                             nothing is folded

Per run, verify does validate_log + fold + every checkpoint (hash and prev_hash
chain) via fold.replay, and reads every line (an unreadable line is a problem
naming the seq it would have had). Across runs: one session_id. The host log:
JSON objects with `type` (str), `body` (dict) and `hseq` gapless from 1.

The on-disk layout is spelled here once: RUN_RE / SEGMENT_RE /
HOST_SEGMENT_RE, run_name(n), segment_name(n, host=), segments(dir),
host_segments(events_dir), run_numbers(events_dir), before_resume_name(n)
(agent-api's StateSync and migration and the guest's EventLog write with
them); iter_segment(path, on_bad=) reads one segment file's events (the
guest skips a bad line, StateSync raises).

Stdlib only. Reading files is the point of this module; the fold itself stays
pure (protocol/fold.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from protocol.fold import State, replay as fold_replay, state_hash

RUN_RE = re.compile(r"^r\d{4}$")
SEGMENT_RE = re.compile(r"^\d{6}\.jsonl$")
HOST_SEGMENT_RE = re.compile(r"^host-\d{6}\.jsonl$")
BEFORE_RESUME_RE = re.compile(r"^events\.before-resume-\d+$")   # a log return's moved-aside buffer
FLAT_RUN = "events"


def run_name(n: int) -> str:
    """The directory name of run n: r0001."""
    return f"r{n:04d}"


def segment_name(n: int, *, host: bool = False) -> str:
    """The file name of segment n: 000001.jsonl (host-000001.jsonl for the host log)."""
    return f"{'host-' if host else ''}{n:06d}.jsonl"


def before_resume_name(n: int) -> str:
    """The name a log return moves the guest's old buffer to: events.before-resume-N."""
    return f"{FLAT_RUN}.before-resume-{n}"


class ReplayError(ValueError):
    """A log that cannot be read (layout, I/O, a line that is not a JSON object)."""


def iter_segment(path, on_bad=None) -> Iterator[dict]:
    """The events of one segment file, in file order; blank lines skipped.
    A line that is not JSON goes to on_bad(path, lineno, exc) and is
    skipped; without on_bad it raises ReplayError naming the file and line."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                if on_bad is None:
                    raise ReplayError(f"{path}:{lineno}: line is not JSON ({exc})") from None
                on_bad(path, lineno, exc)


# ---------------------------------------------------------------- reading
def _events_dir(path: Path) -> Path:
    """The directory holding runs / segments: `path/events` when present, else path."""
    sub = path / "events"
    return sub if sub.is_dir() else path


def _files(directory: Path, pattern: re.Pattern) -> list[Path]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and pattern.match(p.name))


def segments(directory) -> list[Path]:
    """A run directory's segment files, in order ([] when it is absent)."""
    return _files(directory, SEGMENT_RE)


def host_segments(events_dir) -> list[Path]:
    """The host log's segment files, in order."""
    return _files(events_dir, HOST_SEGMENT_RE)


def run_numbers(events_dir) -> list[int]:
    """The run numbers under events_dir (its rNNNN directories), in order."""
    events_dir = Path(events_dir)
    if not events_dir.is_dir():
        return []
    return sorted(int(p.name[1:]) for p in events_dir.iterdir()
                  if p.is_dir() and RUN_RE.match(p.name))


def _run_dirs(events_dir: Path) -> list[tuple[str, list[Path]]]:
    """(run name, its segment files) in order. Flat segments directly under
    events_dir are one run named 'events' (it comes first)."""
    runs: list[tuple[str, list[Path]]] = []
    flat = segments(events_dir)
    if flat:
        runs.append((FLAT_RUN, flat))
    for n in run_numbers(events_dir):
        runs.append((run_name(n), segments(events_dir / run_name(n))))
    return runs


def _read_lines(paths: list[Path], label: str, problems: list[str],
                seq_key: str = "seq") -> list[dict]:
    """Every JSON object in the files, in file order. An unreadable line is a
    problem naming the seq it would have had (the previous one + 1)."""
    out: list[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{label}: {path.name}: unreadable ({exc})")
            continue
        lines = text.split("\n")
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            prev = out[-1].get(seq_key) if out else 0
            nxt = prev + 1 if isinstance(prev, int) and not isinstance(prev, bool) else "?"
            torn = lineno == len(lines) and not text.endswith("\n")
            try:
                obj = json.loads(line)
            except ValueError as exc:
                what = "torn last line" if torn else "line is not JSON"
                problems.append(f"{label}: {seq_key} {nxt}: {what} ({path.name}:{lineno}: {exc})")
                continue
            if not isinstance(obj, dict):
                problems.append(f"{label}: {seq_key} {nxt}: line is not a JSON object "
                                f"({path.name}:{lineno})")
                continue
            out.append(obj)
    return out


def _by_seq(events: list[dict]) -> list[dict]:
    """Stable sort by seq when every seq is an int (fold.replay reports the rest)."""
    if all(isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool) for e in events):
        return sorted(events, key=lambda e: e["seq"])
    return events


def _run_files(path: Path) -> list[tuple[str, list[Path]]]:
    """(run name, its files) of a target: a .jsonl file is one run named
    after it, a directory its runs (empty run directories included)."""
    if path.is_file():
        return [(path.name, [path])]
    if not path.is_dir():
        raise ReplayError(f"{path}: no such file or directory")
    return _run_dirs(_events_dir(path))


def _load(path) -> tuple[list[tuple[str, list[dict]]], list[dict], list[str], list[str]]:
    """(runs as (name, events), host records, read errors, layout notes).
    Read errors are unreadable files and lines; layout notes an empty run
    directory or no log at all (load_session tolerates those)."""
    path = Path(path)
    errors: list[str] = []
    notes: list[str] = []
    runs = []
    for name, files in _run_files(path):
        if not files:
            notes.append(f"{name}: run directory holds no segments")
            continue
        runs.append((name, _by_seq(_read_lines(files, name, errors))))
    if path.is_file():
        return runs, [], errors, notes
    host = _read_lines(host_segments(_events_dir(path)), "host", errors, seq_key="hseq")
    if not runs and not host:
        notes.append(f"{path}: no event log (no events/rNNNN/ runs, no NNNNNN.jsonl segments)")
    return runs, host, errors, notes


def iter_runs(path, *, newest_first: bool = False,
              modified_since: float | None = None) -> Iterator[list[dict]]:
    """The runs of a session directory (or the one run of a .jsonl file),
    each a list of events in seq order, read one at a time; the host log is
    not read. `modified_since` (epoch seconds) leaves out a run whose newest
    segment was last written before it: none of its events is newer. Raises
    ReplayError when a file cannot be read or a line is not a JSON object."""
    runs = _run_files(Path(path))
    for name, files in reversed(runs) if newest_first else runs:
        if not files:
            continue
        if modified_since is not None and files[-1].stat().st_mtime < modified_since:
            continue
        errors: list[str] = []
        events = _by_seq(_read_lines(files, name, errors))
        if errors:
            raise ReplayError("; ".join(errors))
        yield events


def load_session(path) -> list[list[dict]]:
    """Every run of iter_runs(path), oldest first."""
    return list(iter_runs(path))


# ---------------------------------------------------------------- across runs
class SessionIndex:
    """What must hold across a session's runs (each run is its own fold):
    every input_id seen (any dedupe) and each task's settle body. Built
    from folded States (add_state, the State's own indexes) or by a cheap
    scan of a run's events (add_events / scan_run: only `input` and
    `settle` events count, nothing is folded or checked)."""

    def __init__(self) -> None:
        self.seen_input_ids: set[str] = set()
        self.settles: dict[str, dict] = {}

    def add_events(self, events) -> "SessionIndex":
        for ev in events:
            etype, body = ev.get("type"), ev.get("body")
            if not isinstance(body, dict):
                continue
            if etype == "input" and isinstance(body.get("input_id"), str):
                self.seen_input_ids.add(body["input_id"])
            elif etype == "settle" and isinstance(body.get("input_id"), str):
                self.settles[body["input_id"]] = body
        return self

    def add_state(self, state: State) -> "SessionIndex":
        self.seen_input_ids |= state.seen_input_ids
        self.settles.update(state.settles_by_input)
        return self

    def scan_run(self, run_dir) -> "SessionIndex":
        """Add one run directory's inputs and settles. A line is parsed only
        when it can be one (it holds `"input"` or `"settle"`); a line that
        is not JSON raises ReplayError, as iter_segment does."""
        for path in segments(run_dir):
            with Path(path).open("rb") as fh:
                for lineno, line in enumerate(fh, 1):
                    if b'"input"' not in line and b'"settle"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError as exc:
                        raise ReplayError(f"{path}:{lineno}: line is not JSON ({exc})") from None
                    if isinstance(ev, dict):
                        self.add_events((ev,))
        return self

    def settled(self, input_id: str) -> dict | None:
        return self.settles.get(input_id)


def session_index(path, runs=None) -> SessionIndex:
    """A SessionIndex of the session at `path` (a session dir or its events/
    dir): every run, or only the run numbers in `runs`. Cheap: no fold."""
    events_dir = _events_dir(Path(path))
    index = SessionIndex()
    for n in run_numbers(events_dir) if runs is None else runs:
        index.scan_run(events_dir / run_name(n))
    return index


# ---------------------------------------------------------------- checks
def _host_problems(records: list[dict]) -> list[str]:
    problems = []
    for i, rec in enumerate(records, 1):
        hseq = rec.get("hseq")
        if not isinstance(hseq, int) or isinstance(hseq, bool) or hseq != i:
            problems.append(f"host: hseq {i}: expected hseq {i} (gapless from 1), got {hseq!r}")
            break
        if not isinstance(rec.get("type"), str) or not rec.get("type"):
            problems.append(f"host: hseq {i}: type must be a non-empty str")
        if not isinstance(rec.get("body"), dict):
            problems.append(f"host: hseq {i}: body must be a dict")
    return problems


def _verify_runs(runs: list[tuple[str, list[dict]]]) -> tuple[list[str], list[dict]]:
    """(problems, per-run results {run, events, state, state_hash, problems})."""
    problems: list[str] = []
    results = []
    session_id = None
    for name, events in runs:
        state, errors = fold_replay(events)
        run_problems = [f"{name}: {e}" for e in errors]
        sid = events[0].get("session_id") if events else None
        if session_id is None:
            session_id = sid
        elif sid is not None and sid != session_id:
            run_problems.append(f"{name}: seq 1: session_id {sid!r} != the session's "
                                f"{session_id!r} (first run)")
        problems.extend(run_problems)
        results.append({"run": name, "events": len(events), "state": state,
                        "state_hash": state_hash(state), "problems": run_problems})
    return problems, results


def verify_session(path) -> list[str]:
    """Problems with the session at `path` (a session dir, its events/ dir or a
    .jsonl file): unreadable lines, validate_log, the fold, every checkpoint
    and the prev_hash chain, per run; one session_id across runs; the host
    log's hseq. [] == clean. Never raises for a bad log."""
    try:
        runs, host, errors, notes = _load(path)
    except (ReplayError, OSError) as exc:
        return [str(exc)]
    more, _ = _verify_runs(runs)
    return errors + notes + more + _host_problems(host)


def find_sessions(root, *, include_before_resume: bool = False) -> list[Path]:
    """Every directory under root (root included) that holds an event log: an
    `events/` dir with run dirs or segments, or flat segments of its own when
    it is itself named `events`. A `.jsonl` file root is returned as is. With
    include_before_resume, also every `events.before-resume-N` buffer a log
    return moved aside (flat segments, verified as a log of its own)."""
    root = Path(root)
    if root.is_file():
        return [root]
    dirs = sorted(p for p in root.rglob("*") if p.is_dir())
    found = [root] if root.name == "events" and root.is_dir() and _run_dirs(root) else []
    for d in [root, *dirs]:
        if d.name == "events" or RUN_RE.match(d.name):
            continue
        ev = d / "events"
        if ev.is_dir() and (_run_dirs(ev) or host_segments(ev)):
            found.append(d)
    if include_before_resume:
        found.extend(d for d in dirs if BEFORE_RESUME_RE.match(d.name))
    return found


def summarize(state: State) -> dict:
    """What `replay` prints for one run."""
    open_calls = sorted(cid for cid, c in state.open_calls.items() if c.get("result") is None)
    settle = state.last_settle or {}
    return {
        "session_id": state.session_id, "user": state.user, "tenant": state.tenant,
        "last_seq": state.last_seq, "turns": len(state.turns),
        "tools": len(state.tools), "calls": len(state.open_calls), "open_calls": open_calls,
        "prefix_items": len(state.committed), "staged": len(state.staged_suffix),
        "inputs_seen": len(state.seen_input_ids),
        "usage": dict(state.usage), "parked": state.parked,
        "last_settle": settle.get("outcome"), "state_hash": state_hash(state),
    }


# ---------------------------------------------------------------- CLI
def _targets(args) -> list[Path]:
    if not getattr(args, "all", False):
        return [Path(t) for t in args.targets]
    out: list[Path] = []
    for root in args.targets:
        out.extend(find_sessions(root))
    return out


def _cmd_replay(args) -> int:
    report, bad = [], 0
    for target in _targets(args):
        try:
            runs, _host, errors, notes = _load(target)
            problems = errors + notes
        except ReplayError as exc:
            runs, problems = [], [str(exc)]
        run_problems, results = _verify_runs(runs)
        problems = problems + run_problems
        bad += bool(problems)
        report.append({"target": str(target), "ok": not problems, "problems": problems,
                       "runs": [{"run": r["run"], **summarize(r["state"])} for r in results]})
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for t in report:
            for r in t["runs"]:
                print(f"{t['target']}: {r['run']}: session {r['session_id']} seq 1..{r['last_seq']} "
                      f"turns={r['turns']} calls={r['calls']} open={len(r['open_calls'])} "
                      f"settle={r['last_settle']} parked={r['parked']} "
                      f"state_hash={r['state_hash']}")
            for p in t["problems"]:
                print(f"{t['target']}: {p}")
    return 1 if bad else 0


def _cmd_verify(args) -> int:
    targets = _targets(args)
    report = [{"target": str(t), "problems": verify_session(t)} for t in targets]
    failed = [t for t in report if t["problems"]]
    if args.json:
        print(json.dumps({"ok": not failed, "targets": len(report), "failed": len(failed),
                          "results": report}, indent=2, sort_keys=True))
    else:
        for t in failed:
            for p in t["problems"]:
                print(f"{t['target']}: {p}")
        print(f"verified {len(report)} target(s): {len(report) - len(failed)} ok, "
              f"{len(failed)} failed", file=sys.stderr)
    if getattr(args, "all", False) and not targets:
        print("no session found", file=sys.stderr)
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m protocol.replay",
                                     description="Replay or verify harness event logs.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("replay", "fold and print a state summary per run"),
                           ("verify", "validate, fold and check the checkpoint chain")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("targets", nargs="+", metavar="target",
                       help="a session dir, its events/ dir or a .jsonl file "
                            "(with --all: roots to search)")
        p.add_argument("--all", action="store_true",
                       help="treat targets as roots and use every session dir under them")
        p.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)
    return _cmd_replay(args) if args.command == "replay" else _cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
