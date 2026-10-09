"""protocol.replay — load_session / verify_session and the replay/verify CLI
(a replay mismatch or a broken checkpoint chain is a hard error naming
the seq)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from protocol.replay import ReplayError, find_sessions, load_session, main, verify_session

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
NAMES = ("text_answer", "device_tool_late_duplicate", "mcp_tool_via_gateway")

# these tests write broken logs on purpose: the conftest check would flag them
pytestmark = pytest.mark.no_log_verify("tampered logs on purpose")


def golden(name: str) -> list[dict]:
    with open(GOLDEN / f"{name}.jsonl", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_run(session: Path, run: str, events: list[dict], segment_events: int = 1000) -> None:
    """events/<run>/NNNNNN.jsonl the way state_sync writes them."""
    d = session / "events" / run
    d.mkdir(parents=True, exist_ok=True)
    for ev in events:
        n = (ev["seq"] - 1) // segment_events + 1
        with open(d / f"{n:06d}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")


def by_seq(events: list[dict], seq: int) -> dict:
    return next(e for e in events if e["seq"] == seq)


def seqs_named(problems: list[str]) -> list[int]:
    out = []
    for p in problems:
        if "seq " in p:
            out.append(int(p.split("seq ", 1)[1].split(":", 1)[0]))
    return out


# ---------------------------------------------------------------- goldens
@pytest.mark.parametrize("name", NAMES)
def test_goldens_verify_as_files(name):
    assert verify_session(GOLDEN / f"{name}.jsonl") == []


@pytest.mark.parametrize("name", NAMES)
def test_goldens_verify_as_session_dirs_split_in_segments(tmp_path, name):
    write_run(tmp_path / "s", "r0001", golden(name), segment_events=4)
    assert len(list((tmp_path / "s" / "events" / "r0001").iterdir())) > 1
    assert verify_session(tmp_path / "s") == []
    assert verify_session(tmp_path / "s" / "events") == []
    assert load_session(tmp_path / "s") == [golden(name)]


def test_cli_verify_goldens_exit_zero(capsys):
    assert main(["verify", *(str(GOLDEN / f"{n}.jsonl") for n in NAMES)]) == 0
    assert "3 ok, 0 failed" in capsys.readouterr().err


def test_cli_replay_prints_state_hash(capsys):
    from protocol.fold import fold, state_hash
    assert main(["replay", "--json", str(GOLDEN / "device_tool_late_duplicate.jsonl")]) == 0
    report = json.loads(capsys.readouterr().out)
    run = report[0]["runs"][0]
    assert run["state_hash"] == state_hash(fold(golden("device_tool_late_duplicate")))
    assert run["turns"] == 2 and run["last_settle"] == "done" and run["open_calls"] == []


def test_module_entry_point_runs():
    out = subprocess.run([sys.executable, "-m", "protocol.replay", "verify",
                          str(GOLDEN / "text_answer.jsonl")],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------- tampering
def test_altered_event_fails_at_the_checkpoint_naming_its_seq(tmp_path):
    events = golden("device_tool_late_duplicate")
    by_seq(events, 10)["body"]["content"] = "[]"          # the tool result, rewritten
    write_run(tmp_path, "r0001", events)
    problems = verify_session(tmp_path)
    assert problems and seqs_named(problems) == [18]
    assert "checkpoint state_hash mismatch" in problems[0]
    assert problems[0].startswith("r0001: seq 18: ")


def test_bad_checkpoint_hash_names_the_checkpoint_seq(tmp_path):
    events = golden("text_answer")
    cp = events[-1]
    cp["body"]["state_hash"] = "0" * 64
    write_run(tmp_path, "r0001", events)
    problems = verify_session(tmp_path)
    assert seqs_named(problems) == [cp["seq"]]


def test_gap_names_the_seq_after_it(tmp_path):
    events = [e for e in golden("device_tool_late_duplicate") if e["seq"] != 9]
    write_run(tmp_path, "r0001", events)
    problems = verify_session(tmp_path)
    assert problems and min(seqs_named(problems)) == 10
    assert any("expected seq 9" in p for p in problems)


def test_broken_prev_hash_chain_names_the_seq(tmp_path):
    events = golden("mcp_tool_via_gateway")
    cp = events[-1]
    cp["body"]["prev_hash"] = "f" * 64
    write_run(tmp_path, "r0001", events)
    problems = verify_session(tmp_path)
    assert seqs_named(problems) == [cp["seq"]]
    assert "does not continue the chain" in problems[0]


def test_unreadable_line_names_the_seq_it_would_have_had(tmp_path):
    write_run(tmp_path, "r0001", golden("text_answer")[:3])
    with open(tmp_path / "events" / "r0001" / "000001.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq": 4, "type": "turn_')            # torn tail
    problems = verify_session(tmp_path)
    assert problems == [problems[0]] and "r0001: seq 4: torn last line" in problems[0]
    with pytest.raises(ReplayError):
        load_session(tmp_path)


# ---------------------------------------------------------------- multi-run sessions
def test_multi_run_session_verifies_each_run_with_its_own_fold(tmp_path):
    first = golden("text_answer")
    second = golden("text_answer")
    second[1]["body"]["input_id"] = "in-run2"          # another task in the second run
    by_seq(second, 6)["body"]["input_id"] = "in-run2"  # its settle
    from protocol.fold import fold, state_hash
    from protocol.canonical import hash_value
    # re-seal the checkpoint of the edited run
    cp = second[-1]
    cp["body"]["prev_hash"] = hash_value(second[0])
    cp["body"]["state_hash"] = state_hash(fold(second, upto_seq=cp["body"]["through_seq"]))
    write_run(tmp_path, "r0001", first)
    write_run(tmp_path, "r0002", second)
    assert verify_session(tmp_path) == []
    runs = load_session(tmp_path)
    assert [len(r) for r in runs] == [len(first), len(second)]
    assert runs[1][1]["body"]["input_id"] == "in-run2"


def test_multi_run_problem_names_the_run_and_seq(tmp_path):
    write_run(tmp_path, "r0001", golden("text_answer"))
    bad = golden("text_answer")
    bad[-1]["body"]["state_hash"] = "1" * 64
    write_run(tmp_path, "r0002", bad)
    problems = verify_session(tmp_path)
    assert len(problems) == 1 and problems[0].startswith(f"r0002: seq {bad[-1]['seq']}: ")


def test_runs_of_one_session_share_the_session_id(tmp_path):
    write_run(tmp_path, "r0001", golden("text_answer"))
    write_run(tmp_path, "r0002", golden("mcp_tool_via_gateway"))
    problems = verify_session(tmp_path)
    assert problems and problems[0].startswith("r0002: seq 1: session_id")


def test_host_log_hseq_is_checked(tmp_path):
    write_run(tmp_path, "r0001", golden("text_answer"))
    host = tmp_path / "events" / "host-000001.jsonl"
    recs = [{"ts": "t", "hseq": 1, "session_id": "s", "type": "gateway", "body": {}},
            {"ts": "t", "hseq": 3, "session_id": "s", "type": "input", "body": {}}]
    host.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    assert verify_session(tmp_path) == ["host: hseq 2: expected hseq 2 (gapless from 1), got 3"]


def test_missing_or_empty_session(tmp_path):
    assert verify_session(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    assert verify_session(tmp_path / "empty")


# ---------------------------------------------------------------- --all over a tree
def _tree(root: Path) -> None:
    write_run(root / "u1" / "good", "r0001", golden("text_answer"))
    write_run(root / "u1" / "good2", "r0001", golden("mcp_tool_via_gateway"))
    bad = [e for e in golden("device_tool_late_duplicate") if e["seq"] != 12]
    write_run(root / "u2" / "bad", "r0001", bad)
    (root / "u2" / "legacy").mkdir(parents=True)
    (root / "u2" / "legacy" / "history.jsonl").write_text("{}\n", encoding="utf-8")


def test_find_sessions_skips_dirs_without_an_event_log(tmp_path):
    _tree(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in find_sessions(tmp_path)] == [
        "u1/good", "u1/good2", "u2/bad"]


def test_cli_verify_all_reports_failing_sessions_with_seq(tmp_path, capsys):
    _tree(tmp_path)
    assert main(["verify", "--all", str(tmp_path)]) == 1
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert lines and all(line.startswith(str(tmp_path / "u2" / "bad") + ": r0001: seq ")
                         for line in lines)
    assert "seq 13: expected seq 12" in out.out
    assert "2 ok, 1 failed" in out.err


def test_cli_verify_all_json(tmp_path, capsys):
    _tree(tmp_path)
    assert main(["verify", "--all", "--json", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["targets"] == 3 and report["failed"] == 1 and not report["ok"]
    failed = [r for r in report["results"] if r["problems"]]
    assert failed[0]["target"].endswith("bad")


def test_cli_verify_all_clean_tree(tmp_path):
    write_run(tmp_path / "a", "r0001", golden("text_answer"))
    assert main(["verify", "--all", str(tmp_path)]) == 0


# ---------------------------------------------------------------- the CI hook (conftest)
def test_ci_hook_checks_fake_host_logs_per_guest_run():
    from types import SimpleNamespace
    from tests.conftest import host_log_problems
    two_runs = golden("text_answer") + golden("text_answer")
    assert host_log_problems(SimpleNamespace(events=two_runs)) == []
    broken = golden("text_answer")
    del broken[3]
    problems = host_log_problems(SimpleNamespace(events=golden("text_answer") + broken))
    assert problems and problems[0].startswith("run 2: seq 5: ")


def test_ci_hook_finds_session_dirs_and_resume_buffers(tmp_path):
    from tests.conftest import produced_logs
    write_run(tmp_path / "sess", "r0001", golden("text_answer"))
    flat = tmp_path / "log" / "events"
    flat.mkdir(parents=True)
    (flat / "000001.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "log" / "events.before-resume-1").mkdir()
    got = sorted(p.relative_to(tmp_path).as_posix() for p in produced_logs(tmp_path))
    assert got == ["log", "log/events.before-resume-1", "sess"]
