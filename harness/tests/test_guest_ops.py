"""guest.ops: OperationManager — pool, per-file queue, bash process groups,
timeout/abort, spill and quota, workspace_tree_id."""
import dataclasses
import hashlib
import os
import queue
import threading
import time

import pytest

from guest import tools
from guest.config import Limits
from guest.ops import OperationManager, workspace_tree_id

RESULT_KEYS = {"ok", "content", "state", "duration_ms", "spill"}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    return tmp_path


@pytest.fixture
def inbound():
    return queue.Queue()


@pytest.fixture
def mgr(ws, inbound):
    m = OperationManager(ws, Limits(spill_threshold=200, spill_quota_bytes=10_000), inbound)
    yield m
    m.abort_all()
    m.shutdown()


def take(inbound, n=1, timeout=10.0):
    """Collect n ("op", call_id, result) items as {call_id: result}, checking the shape."""
    out = {}
    deadline = time.monotonic() + timeout
    while len(out) < n:
        kind, call_id, result = inbound.get(timeout=max(0.01, deadline - time.monotonic()))
        assert kind == "op"
        assert set(result) == RESULT_KEYS
        assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
        assert result["state"] in ("done", "failed", "timeout", "killed")
        out[call_id] = result
    return out


def pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ----------------------------------------------------------------- basics

def test_file_tools_post_op_items(mgr, inbound, ws):
    mgr.submit("c1", "read", {"path": "a.txt"})
    mgr.submit("c2", "write", {"path": "b/c.txt", "content": "x"})
    r = take(inbound, 2)
    assert r["c1"] == {"ok": True, "content": "     1\talpha", "state": "done",
                       "duration_ms": r["c1"]["duration_ms"], "spill": None}
    assert r["c2"]["ok"] and (ws / "b/c.txt").read_text() == "x"
    # independent tools run concurrently, so list only after the write is done
    mgr.submit("c3", "ls", {})
    r.update(take(inbound, 1))
    assert r["c3"]["content"].splitlines() == ["a.txt", "b/"]
    assert mgr.running() == set()


def test_tool_error_is_failed_not_exception(mgr, inbound):
    mgr.submit("c1", "read", {"path": "missing"})
    mgr.submit("c2", "edit", {"path": "a.txt", "old_text": "zzz", "new_text": ""})
    r = take(inbound, 2)
    assert r["c1"]["state"] == "failed" and not r["c1"]["ok"]
    assert r["c1"]["content"].startswith("Error: file not found")
    assert r["c2"]["state"] == "failed" and "not found" in r["c2"]["content"]


def test_invalid_args_unknown_tool_and_question_fail_fast(mgr, inbound):
    mgr.submit("c1", "read", {"path": "../etc/passwd"})
    mgr.submit("c2", "nope", {})
    mgr.submit("c3", "question", {"text": "hi"})
    mgr.submit("c4", "write", {"path": "a.txt"})
    r = take(inbound, 4)
    for cid in r:
        assert r[cid]["state"] == "failed" and r[cid]["content"].startswith("Error:")
    assert "escapes" in r["c1"]["content"]
    assert "unknown tool" in r["c2"]["content"]
    assert "coordinator" in r["c3"]["content"]
    assert "content: missing" in r["c4"]["content"]


def test_duplicate_call_id_is_rejected(mgr, inbound, ws):
    mgr.submit("dup", "bash", {"command": "sleep 0.3"})
    mgr.submit("dup", "read", {"path": "a.txt"})
    r = take(inbound, 1)
    assert r["dup"]["state"] == "failed" and "duplicate call_id" in r["dup"]["content"]
    r = take(inbound, 1)
    assert r["dup"]["state"] == "done"


def test_programming_error_in_impl_is_reported_failed(mgr, inbound, monkeypatch):
    def boom(workspace, args, limits):
        raise RuntimeError("bug")
    monkeypatch.setitem(tools.SPECS, "ls", dataclasses.replace(tools.SPECS["ls"], impl=boom))
    mgr.submit("c1", "ls", {})
    r = take(inbound, 1)
    assert r["c1"]["state"] == "failed" and "internal error in ls" in r["c1"]["content"]
    assert "RuntimeError('bug')" in r["c1"]["content"]
    assert mgr.running() == set()


# ------------------------------------------------------------ concurrency

def test_independent_ops_run_concurrently(mgr, inbound):
    t0 = time.monotonic()
    for i in range(4):
        mgr.submit(f"b{i}", "bash", {"command": "sleep 0.4; echo hi"})
    assert mgr.running() == {"b0", "b1", "b2", "b3"}
    r = take(inbound, 4)
    elapsed = time.monotonic() - t0
    assert all(v["ok"] and v["content"] == "hi\n" for v in r.values())
    assert elapsed < 1.2, elapsed       # four × 0.4 s serial would be 1.6 s


def test_same_file_writes_serialise_in_order(mgr, inbound, ws, monkeypatch):
    events = []
    lock = threading.Lock()
    real = tools.SPECS["write"].impl

    def slow_write(workspace, args, limits):
        with lock:
            events.append(("start", args["path"], args["content"]))
        time.sleep(0.15)
        out = real(workspace, args, limits)
        with lock:
            events.append(("end", args["path"], args["content"]))
        return out

    monkeypatch.setitem(tools.SPECS, "write", dataclasses.replace(tools.SPECS["write"], impl=slow_write))
    for i in range(3):
        mgr.submit(f"same{i}", "write", {"path": "same.txt", "content": str(i)})
        mgr.submit(f"other{i}", "write", {"path": f"other{i}.txt", "content": str(i)})
    r = take(inbound, 6)
    assert all(v["ok"] for v in r.values())
    assert (ws / "same.txt").read_text() == "2"
    same = [e for e in events if e[1] == "same.txt"]
    # strictly start,end,start,end,start,end in submission order
    assert same == [("start", "same.txt", "0"), ("end", "same.txt", "0"),
                    ("start", "same.txt", "1"), ("end", "same.txt", "1"),
                    ("start", "same.txt", "2"), ("end", "same.txt", "2")]
    # the other files overlapped with the chain: some started before same0 ended
    first_end = events.index(("end", "same.txt", "0"))
    assert any(e[0] == "start" and e[1] != "same.txt" for e in events[:first_end])


def test_chained_edits_on_one_file_all_apply(mgr, inbound, ws):
    (ws / "chain.txt").write_text("v0")
    for i in range(12):
        mgr.submit(f"e{i}", "edit", {"path": "chain.txt", "old_text": f"v{i}", "new_text": f"v{i + 1}"})
    r = take(inbound, 12)
    assert all(v["ok"] for v in r.values()), {k: v["content"] for k, v in r.items() if not v["ok"]}
    assert (ws / "chain.txt").read_text() == "v12"


def test_patch_serialises_with_edits_on_its_files(mgr, inbound, ws):
    (ws / "p.txt").write_text("one\n")
    mgr.submit("w", "write", {"path": "p.txt", "content": "two\n"})
    mgr.submit("p", "apply_patch", {"patch": "--- a/p.txt\n+++ b/p.txt\n@@ -1 +1 @@\n-two\n+three\n"})
    mgr.submit("e", "edit", {"path": "./p.txt", "old_text": "three", "new_text": "four"})
    r = take(inbound, 3)
    assert all(v["ok"] for v in r.values()), r
    assert (ws / "p.txt").read_text() == "four\n"


def test_queued_serialised_ops_do_not_block_other_work(ws, inbound, monkeypatch):
    m = OperationManager(ws, Limits(), inbound, pool_size=1)
    try:
        real = tools.SPECS["write"].impl

        def slow(workspace, args, limits):
            time.sleep(0.3)
            return real(workspace, args, limits)
        monkeypatch.setitem(tools.SPECS, "write", dataclasses.replace(tools.SPECS["write"], impl=slow))
        for i in range(3):
            m.submit(f"w{i}", "write", {"path": "q.txt", "content": str(i)})
        m.submit("r", "ls", {})
        r = take(inbound, 4)
        assert r["r"]["ok"] and (ws / "q.txt").read_text() == "2"
    finally:
        m.shutdown()


# ------------------------------------------------------------------- bash

def test_bash_combines_stdout_stderr_and_reports_exit_code(mgr, inbound, ws):
    mgr.submit("c1", "bash", {"command": "echo out; echo err 1>&2; pwd"})
    mgr.submit("c2", "bash", {"command": "echo partial; exit 3"})
    r = take(inbound, 2)
    assert r["c1"]["ok"] and r["c1"]["state"] == "done"
    assert r["c1"]["content"] == f"out\nerr\n{ws.resolve()}\n"
    assert not r["c2"]["ok"] and r["c2"]["state"] == "failed"
    assert r["c2"]["content"] == "partial\n[exit code 3]"


def test_bash_timeout_kills_the_whole_process_group(mgr, inbound):
    t0 = time.monotonic()
    mgr.submit("t", "bash", {"command": "echo $$; sleep 30 & wait", "timeout_s": 0.5})
    r = take(inbound, 1)
    assert time.monotonic() - t0 < 5
    res = r["t"]
    assert res["state"] == "timeout" and not res["ok"]
    assert "[timeout after 0.5s: process group killed]" in res["content"]
    pgid = int(res["content"].split("\n")[0])        # bash's pid == its pgid (new session)
    assert wait_until(lambda: not pgid_alive(pgid)), "sleep child survived the timeout"
    assert mgr.running() == set()


def test_bash_default_timeout_comes_from_limits(ws, inbound):
    m = OperationManager(ws, Limits(op_timeout_s={"guest": 0.3, "host": 1, "device": 1, "mcp": 1}), inbound)
    try:
        m.submit("t", "bash", {"command": "sleep 5"})
        r = take(inbound, 1)
        assert r["t"]["state"] == "timeout"
    finally:
        m.shutdown()


def test_abort_all_kills_running_and_queued(mgr, inbound, ws, monkeypatch):
    mgr.submit("b1", "bash", {"command": "echo $$; sleep 30 & wait"})
    mgr.submit("b2", "bash", {"command": "sleep 30"})
    (ws / "s.txt").write_text("")
    real = tools.SPECS["write"].impl

    def slow(workspace, args, limits):
        time.sleep(0.4)
        return real(workspace, args, limits)
    monkeypatch.setitem(tools.SPECS, "write", dataclasses.replace(tools.SPECS["write"], impl=slow))
    mgr.submit("w1", "write", {"path": "s.txt", "content": "1"})
    mgr.submit("w2", "write", {"path": "s.txt", "content": "2"})   # queued behind w1
    assert wait_until(lambda: all(j.proc is not None for j in list(mgr._jobs.values()) if j.name == "bash"))
    assert mgr.running() == {"b1", "b2", "w1", "w2"}
    aborted = mgr.abort_all()
    assert aborted == {"b1", "b2", "w1", "w2"}
    r = take(inbound, 4)
    assert all(v["state"] == "killed" and not v["ok"] for v in r.values()), r
    pgid = int(r["b1"]["content"].split("\n")[0])
    assert wait_until(lambda: not pgid_alive(pgid))
    assert "aborted before it ran" in r["w2"]["content"]
    assert mgr.running() == set()
    # the manager keeps working afterwards
    mgr.submit("after", "bash", {"command": "echo ok"})
    assert take(inbound, 1)["after"]["content"] == "ok\n"


# ------------------------------------------------------------------ spill

def test_spill_above_threshold_with_sha256(mgr, inbound, ws):
    assert mgr.spill_threshold == 200
    mgr.submit("big", "bash", {"command": "seq 1 300"})
    r = take(inbound, 1)["big"]
    full = "".join(f"{i}\n" for i in range(1, 301))
    assert r["ok"] and r["state"] == "done"
    spill = r["spill"]
    assert spill["path"] == str(ws / ".harness" / "spill" / "big.txt")
    assert spill["bytes"] == len(full.encode())
    assert spill["sha256"] == hashlib.sha256(full.encode()).hexdigest()
    assert (ws / ".harness" / "spill" / "big.txt").read_text() == full
    head, note = r["content"].rsplit("\n", 1)
    assert head == full[:200]
    assert note == f"[truncated: {len(full) - 200} chars; spill big]"


def test_no_spill_at_or_below_threshold(mgr, inbound):
    mgr.submit("c", "write", {"path": "x.txt", "content": "y"})
    mgr.submit("edge", "bash", {"command": "printf '%0.s-' $(seq 1 200)"})
    r = take(inbound, 2)
    assert r["c"]["spill"] is None and r["edge"]["spill"] is None
    assert len(r["edge"]["content"]) == 200


def test_spill_quota_exhaustion(ws, inbound):
    m = OperationManager(ws, Limits(spill_threshold=100, spill_quota_bytes=1500), inbound)
    try:
        for cid in ("s1", "s2", "s3"):
            m.submit(cid, "bash", {"command": "seq 1 300"})     # 1092 bytes each
        r = take(inbound, 3)
        spilled = [c for c, v in r.items() if v["spill"] is not None]
        lost = [c for c, v in r.items() if v["spill"] is None]
        assert len(spilled) == 1 and len(lost) == 2
        for c in lost:
            assert r[c]["content"].endswith("spill quota exhausted, full output lost]")
            assert not (ws / ".harness" / "spill" / f"{c}.txt").exists()
        assert (ws / ".harness" / "spill" / f"{spilled[0]}.txt").exists()
    finally:
        m.shutdown()


def test_spill_quota_counts_existing_files(ws, inbound):
    d = ws / ".harness" / "spill"
    d.mkdir(parents=True)
    (d / "old.txt").write_bytes(b"x" * 1400)
    m = OperationManager(ws, Limits(spill_threshold=100, spill_quota_bytes=1500), inbound)
    try:
        m.submit("s", "bash", {"command": "seq 1 300"})
        assert take(inbound, 1)["s"]["spill"] is None
    finally:
        m.shutdown()


# ------------------------------------------------------- workspace_tree_id

def test_workspace_tree_id_is_stable_and_ignores_harness_dir(ws, mgr):
    a = mgr.workspace_tree_id()
    assert a == workspace_tree_id(ws) == mgr.workspace_tree_id()
    assert len(a) == 64 and int(a, 16)
    (ws / ".harness" / "spill").mkdir(parents=True)
    (ws / ".harness" / "spill" / "x.txt").write_text("noise")
    assert mgr.workspace_tree_id() == a


def test_workspace_tree_id_changes_on_write_rename_and_content(ws, inbound, mgr):
    a = mgr.workspace_tree_id()
    mgr.submit("w", "write", {"path": "new.txt", "content": "n"})
    take(inbound, 1)
    b = mgr.workspace_tree_id()
    assert b != a
    (ws / "new.txt").write_text("m")
    c = mgr.workspace_tree_id()
    assert c not in (a, b)
    (ws / "new.txt").rename(ws / "moved.txt")
    assert mgr.workspace_tree_id() not in (a, b, c)
    (ws / "moved.txt").unlink()
    assert mgr.workspace_tree_id() == a


def test_workspace_tree_id_matches_known_digest(tmp_path):
    (tmp_path / "f").write_text("hi")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "g").write_text("")
    h = hashlib.sha256()
    for rel, content in (("d/g", b""), ("f", b"hi")):
        h.update(rel.encode() + b"\0" + hashlib.sha256(content).hexdigest().encode() + b"\n")
    assert workspace_tree_id(tmp_path) == h.hexdigest()
    assert workspace_tree_id(tmp_path / "d") != workspace_tree_id(tmp_path)
