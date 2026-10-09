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
  verify_session(path) -> list[str]          problems, [] == clean. Each names the seq:
                                             "<run>: seq N: reason" (run = r0001, ..., or
                                             "events" for a flat log, the file name for a
                                             .jsonl target), "host: hseq N: reason" for the
                                             host log, "<path>: reason" for the layout
  find_sessions(root) -> list[Path]          every directory under root holding an event log
  summarize(state) -> dict                   the replay summary of a folded State

Per run, verify does validate_log + fold + every checkpoint (hash and prev_hash
chain) via fold.replay, and reads every line (an unreadable line is a problem
naming the seq it would have had). Across runs: one session_id. The host log:
JSON objects with `type` (str), `body` (dict) and `hseq` gapless from 1.

Stdlib only. Reading files is the point of this module; the fold itself stays
pure (protocol/fold.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from protocol.fold import State, replay as fold_replay, state_hash

RUN_RE = re.compile(r"^r\d{4}$")
SEGMENT_RE = re.compile(r"^\d{6}\.jsonl$")
HOST_SEGMENT_RE = re.compile(r"^host-\d{6}\.jsonl$")
FLAT_RUN = "events"


class ReplayError(ValueError):
    """A log that cannot be read (layout, I/O, a line that is not a JSON object)."""


# ---------------------------------------------------------------- reading
def _events_dir(path: Path) -> Path:
    """The directory holding runs / segments: `path/events` when present, else path."""
    sub = path / "events"
    return sub if sub.is_dir() else path


def _segments(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file() and SEGMENT_RE.match(p.name))


def _run_dirs(events_dir: Path) -> list[tuple[str, list[Path]]]:
    """(run name, its segment files) in order. Flat segments directly under
    events_dir are one run named 'events' (it comes first)."""
    runs: list[tuple[str, list[Path]]] = []
    flat = _segments(events_dir)
    if flat:
        runs.append((FLAT_RUN, flat))
    for d in sorted(p for p in events_dir.iterdir() if p.is_dir() and RUN_RE.match(p.name)):
        runs.append((d.name, _segments(d)))
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


def _load(path) -> tuple[list[tuple[str, list[dict]]], list[dict], list[str]]:
    """(runs as (name, events), host records, read problems)."""
    path = Path(path)
    problems: list[str] = []
    if path.is_file():
        return [(path.name, _by_seq(_read_lines([path], path.name, problems)))], [], problems
    if not path.is_dir():
        raise ReplayError(f"{path}: no such file or directory")
    events_dir = _events_dir(path)
    runs = []
    for name, segments in _run_dirs(events_dir):
        if not segments:
            problems.append(f"{name}: run directory holds no segments")
            continue
        runs.append((name, _by_seq(_read_lines(segments, name, problems))))
    host_files = sorted(p for p in events_dir.iterdir()
                        if p.is_file() and HOST_SEGMENT_RE.match(p.name))
    host = _read_lines(host_files, "host", problems, seq_key="hseq")
    if not runs and not host:
        problems.append(f"{path}: no event log (no events/rNNNN/ runs, no NNNNNN.jsonl segments)")
    return runs, host, problems


def load_session(path) -> list[list[dict]]:
    """The runs of a session directory (or the one run of a .jsonl file), each
    a list of events in seq order. Raises ReplayError when a file cannot be
    read or a line is not a JSON object."""
    runs, _host, problems = _load(path)
    read_errors = [p for p in problems if "holds no segments" not in p and "no event log" not in p]
    if read_errors:
        raise ReplayError("; ".join(read_errors))
    return [events for _name, events in runs]


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
        runs, host, problems = _load(path)
    except (ReplayError, OSError) as exc:
        return [str(exc)]
    more, _ = _verify_runs(runs)
    return problems + more + _host_problems(host)


def find_sessions(root) -> list[Path]:
    """Every directory under root (root included) that holds an event log: an
    `events/` dir with run dirs or segments, or flat segments of its own when
    it is itself named `events`. A `.jsonl` file root is returned as is."""
    root = Path(root)
    if root.is_file():
        return [root]
    found = [root] if root.name == "events" and root.is_dir() and _run_dirs(root) else []
    for d in sorted([root, *(p for p in root.rglob("*") if p.is_dir())]):
        if d.name == "events" or RUN_RE.match(d.name):
            continue
        ev = d / "events"
        if ev.is_dir() and (_run_dirs(ev) or any(HOST_SEGMENT_RE.match(p.name)
                                                 for p in ev.iterdir())):
            found.append(d)
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
            runs, _host, problems = _load(target)
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
