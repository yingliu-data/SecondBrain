"""LocalSandbox emulation of snapshots: snapshot = freeze the
parked guest process (SIGSTOP) keeping its memory, restore(session) = SIGCONT,
restore(base) = a fresh guest. A stand-in `guest.main` counts in memory and
appends the counter to a file, so "same process, same state" is observable."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.host.sandbox import Sandbox
from app.host.sandbox.firecracker import SandboxError
from app.host.sandbox.local import LOCAL_SNAPSHOT_MARKER, LocalSandbox, _is_zombie

FAKE_GUEST = r'''
import argparse, os, sys, time
p = argparse.ArgumentParser()
p.add_argument("--connect"); p.add_argument("--log-dir"); p.add_argument("--workspace")
p.add_argument("--exit-after", type=float, default=None)
a = p.parse_args()
out = os.path.join(a.log_dir, "ticks")
n = 0
t0 = time.monotonic()
while True:
    n += 1
    with open(out, "a") as fh:
        fh.write(f"{os.getpid()} {n}\n")
    if a.exit_after is not None and time.monotonic() - t0 > a.exit_after:
        sys.exit(0)
    time.sleep(0.02)
'''


@pytest.fixture
def guest_root(tmp_path):
    root = tmp_path / "harness"
    (root / "guest").mkdir(parents=True)
    (root / "guest" / "__init__.py").write_text("")
    (root / "guest" / "main.py").write_text(FAKE_GUEST)
    return root


@pytest.fixture
def session_dir(tmp_path):
    d = tmp_path / "sessions" / "owner" / "default_l1"
    d.mkdir(parents=True)
    return d


def _gone(pid: int, timeout: float = 5.0) -> bool:
    """Dead (or a zombie of this test process that nobody reaped)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if _is_zombie(pid):
            return True
        time.sleep(0.02)
    return False


def _ticks(session_dir: Path) -> list[tuple[int, int]]:
    f = session_dir / "guest" / "ticks"
    if not f.exists():
        return []
    return [tuple(map(int, l.split())) for l in f.read_text().splitlines() if l.strip()]


def _wait_ticks(session_dir: Path, n: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while len(_ticks(session_dir)) < n:
        assert time.monotonic() < deadline, "guest did not tick"
        time.sleep(0.02)


def test_freeze_and_resume_keeps_process_and_state(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root)
    assert isinstance(sb, Sandbox)
    assert sb.has_snapshot()                       # the "base" is always there locally
    sb.restore(session_dir)                        # base -> fresh guest
    assert sb.last_vm_event["op"] == "restore" and sb.last_vm_event["source"] == "base"
    try:
        _wait_ticks(session_dir, 3)
        pid = sb.proc.pid
        snap = session_dir / "vm" / "snapshot"
        sb.snapshot(snap)
        assert not sb.is_alive()                   # like a snapshotted VM
        assert sb.has_snapshot(snap)
        assert json.loads((snap / LOCAL_SNAPSHOT_MARKER).read_text())["pid"] == pid
        frozen_at = len(_ticks(session_dir))
        time.sleep(0.2)
        assert len(_ticks(session_dir)) <= frozen_at + 1   # stopped (one write may be in flight)

        sb.restore(session_dir, snap)
        assert sb.is_alive() and sb.proc.pid == pid
        assert sb.last_vm_event["source"] == "session"
        assert not sb.has_snapshot(snap)           # single-use
        _wait_ticks(session_dir, frozen_at + 3)
        ticks = _ticks(session_dir)
        assert {p for p, _ in ticks} == {pid}
        assert [n for _, n in ticks] == list(range(1, len(ticks) + 1))   # memory kept
    finally:
        sb.stop()
    assert not sb.is_alive()


def test_another_sandbox_object_adopts_the_frozen_guest(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root)
    sb.start(session_dir)
    _wait_ticks(session_dir, 2)
    pid = sb.proc.pid
    snap = session_dir / "vm" / "snapshot"
    sb.snapshot(snap)
    sb2 = LocalSandbox(guest_root=guest_root)      # e.g. a new worker for the session
    try:
        assert sb2.has_snapshot(snap)
        sb2.restore(session_dir, snap)
        assert sb2.is_alive() and sb2.proc.pid == pid
        n = len(_ticks(session_dir))
        _wait_ticks(session_dir, n + 2)
    finally:
        sb2.stop()
    assert not sb2.is_alive()
    sb.proc = None                                  # the first object never saw it end
    assert _gone(pid)


def test_restore_session_when_the_process_is_gone(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root)
    sb.start(session_dir)
    _wait_ticks(session_dir, 1)
    snap = session_dir / "vm" / "snapshot"
    sb.snapshot(snap)
    sb.stop()                                       # kills the frozen guest
    assert not sb.has_snapshot(snap)
    with pytest.raises(SandboxError, match="process gone"):
        sb.restore(session_dir, snap)


def test_snapshot_after_the_guest_exited_is_an_error(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root, extra_args=["--exit-after", "0"])
    sb.start(session_dir)
    sb.proc.wait(timeout=5)
    with pytest.raises(SandboxError, match="exited"):
        sb.snapshot(session_dir / "vm" / "snapshot")
    assert not (session_dir / "vm" / "snapshot" / LOCAL_SNAPSHOT_MARKER).exists()
    sb.stop()


def test_stop_kills_a_frozen_guest(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root)
    sb.start(session_dir)
    _wait_ticks(session_dir, 1)
    pid = sb.proc.pid
    sb.snapshot(session_dir / "vm" / "snapshot")
    sb.stop()
    assert _gone(pid), "frozen guest survived stop()"


def test_restore_base_replaces_a_stale_frozen_guest(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root)
    sb.start(session_dir)
    _wait_ticks(session_dir, 1)
    old = sb.proc.pid
    sb.snapshot(session_dir / "vm" / "snapshot")
    sb.restore(session_dir)                        # death path: base, fresh process
    try:
        assert sb.is_alive() and sb.proc.pid != old
    finally:
        sb.stop()


def test_start_stops_a_stray_guest_of_the_session(guest_root, session_dir, tmp_path):
    """A guest left running by a host that ended without stopping it would take
    the next run's link; a start stops it, and leaves other sessions' guests."""
    other_dir = tmp_path / "sessions" / "owner" / "default_l2"
    other_dir.mkdir(parents=True)
    other = LocalSandbox(guest_root=guest_root)
    other.start(other_dir)
    old = LocalSandbox(guest_root=guest_root)
    old.start(session_dir)
    _wait_ticks(session_dir, 1)
    stray, old.proc = old.proc.pid, None            # the host that started it is gone
    sb = LocalSandbox(guest_root=guest_root)
    try:
        sb.start(session_dir)
        assert _gone(stray), "stray guest survived the start"
        assert sb.is_alive() and sb.proc.pid != stray
        assert other.is_alive()
    finally:
        sb.stop()
        other.stop()


def test_start_stops_a_frozen_stray_guest(guest_root, session_dir):
    old = LocalSandbox(guest_root=guest_root)
    old.start(session_dir)
    _wait_ticks(session_dir, 1)
    stray = old.proc.pid
    old.snapshot(session_dir / "vm" / "snapshot")
    old._frozen = None                              # forgotten while frozen
    sb = LocalSandbox(guest_root=guest_root)
    try:
        sb.start(session_dir)
        assert _gone(stray), "frozen stray guest survived the start"
    finally:
        sb.stop()


def test_restore_while_running_is_an_error(guest_root, session_dir):
    sb = LocalSandbox(guest_root=guest_root)
    sb.start(session_dir)
    try:
        with pytest.raises(SandboxError, match="already started"):
            sb.restore(session_dir)
    finally:
        sb.stop()


HOST_SECRETS = ("OPENAI_API_KEY", "API_SECRET_KEY", "INTERNAL_TOKEN", "CF_TUNNEL_TOKEN", "EMAIL_ACCOUNTS")


def test_guest_environment_has_no_host_secrets(guest_root, session_dir, monkeypatch):
    """The host's MONGODB_* variables (the ast_index skill's), the LLM vendor keys, the API
    key, the tokens (gateway bearer, tunnel) and the email accounts never reach the guest's shell."""
    monkeypatch.setenv("MONGODB_PASSWORD", "secret")
    for name in HOST_SECRETS:
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("SB_TEST_VISIBLE", "1")
    (guest_root / "guest" / "main.py").write_text(
        "import json, os, sys, time\n"
        "log = sys.argv[sys.argv.index('--log-dir') + 1]\n"
        "json.dump(sorted(os.environ), open(os.path.join(log, 'env.json'), 'w'))\n"
        "time.sleep(30)\n")
    sb = LocalSandbox(guest_root=guest_root)
    sb.start(session_dir)
    try:
        out = session_dir / "guest" / "env.json"
        deadline = time.monotonic() + 5
        while not (out.exists() and out.read_text().endswith("]")):
            assert time.monotonic() < deadline, "guest did not write its environment"
            time.sleep(0.02)
        names = json.loads(out.read_text())
        assert "SB_TEST_VISIBLE" in names and not any(n.startswith("MONGODB_") for n in names)
        assert not set(HOST_SECRETS) & set(names)
    finally:
        sb.stop()
