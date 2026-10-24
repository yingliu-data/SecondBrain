"""The terminal chat, app/cli.py (plan feat/52 step 3, issue #52): the real
Workers on the local sandbox (the guest as a subprocess) against a scripted
upstream LLM, as test_host_e2e.py. The turns run in-process; start, end of
input, Ctrl-C and a bad config run the CLI as a subprocess."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import cli
from app.host.sandbox.local import LocalSandbox
from app.host.worker import Workers
from app.session.dir_store import DirStore
from host_fakes import FakeRegistry, FakeTenant, FakeUpstreamLLM, users_root, worker_kwargs  # noqa: F401

AGENT_API = Path(__file__).resolve().parents[1]
GUEST_ROOT = AGENT_API.parent / "harness"
FLUSH = object()


class Out:
    """stdout for run_turn: each write, and each flush as FLUSH."""

    def __init__(self):
        self.parts: list = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        self.parts.append(FLUSH)


def lines(*texts) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    for t in texts:
        q.put_nowait(t + "\n")
    q.put_nowait(None)
    return q


def guests(root: Path) -> list[str]:
    """The live guest processes of sessions under `root` (a zombie has no cmdline)."""
    found = []
    for p in Path("/proc").iterdir():
        try:
            cmd = (p / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if "guest.main" in cmd and str(root) in cmd:
            found.append(p.name)
    return found


@pytest.fixture
def upstream():
    u = FakeUpstreamLLM([{"content": "hello there"}]).start()
    yield u
    u.stop()


@pytest.fixture
def local_chat(tmp_path, users_root, monkeypatch, upstream):  # noqa: F811
    """chat.workers on the local sandbox and the fake upstream; call it again for a new run."""
    from app.routes import chat
    monkeypatch.setattr(chat, "_registry", FakeRegistry())

    def new_run():
        monkeypatch.setattr(chat, "workers", Workers(
            sandbox_factory=lambda: LocalSandbox(GUEST_ROOT), sessions=DirStore(tmp_path / "sessions"),
            **worker_kwargs(llm_url=upstream.url, hello_timeout_s=20, heartbeat_s=1, wall_clock_s=60)))
    new_run()
    return new_run


async def test_one_turn_streams_reply(tmp_path, local_chat, upstream):
    """Issue #52: the reply streams to the terminal as it arrives: each token is
    written and flushed on its own, before the turn's closing newline."""
    upstream.responses = [{"content": "one two three"}]
    out = Out()
    try:
        await cli.run_turn(FakeTenant(), "s1", "count", out)
    finally:
        from app.routes import chat
        await chat.workers.shutdown()
    writes = [p for p in out.parts if p is not FLUSH]
    assert "".join(writes).split() == ["one", "two", "three"]
    assert len([w for w in writes if w.strip()]) > 1                  # streamed, not one block
    assert all(out.parts[i + 1] is FLUSH for i, p in enumerate(out.parts) if p is not FLUSH)
    assert writes[-1] == "\n"
    assert guests(tmp_path) == []


async def test_follow_up_and_resume(tmp_path, local_chat, upstream):
    """Issue #52: follow-up messages share one session, and a later run on the
    same session id sends the earlier history to the LLM."""
    upstream.responses = [{"content": "r1"}, {"content": "r2"}, {"content": "r3"}]
    await cli.chat_loop(FakeTenant(), "s1", lines("first", "second"), Out())
    assert "first" in json.dumps(upstream.requests[1]["messages"])
    assert guests(tmp_path) == []

    local_chat()                                     # a second run, from the log
    await cli.chat_loop(FakeTenant(), "s1", lines("third"), Out())
    history = json.dumps(upstream.requests[-1]["messages"])
    assert all(t in history for t in ("first", "r1", "second", "r2", "third"))
    assert guests(tmp_path) == []


def run_cli(tmp_path, upstream, *args, **env) -> subprocess.Popen:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"llm": {"vendor": "vllm", "model": "m"}}))
    return subprocess.Popen(
        [sys.executable, "-m", "app.cli", "--config", str(config), *args], cwd=AGENT_API,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "LLM_URL": upstream.url, "SESSIONS_ROOT": str(tmp_path / "sessions"),
             "USERS_ROOT": str(tmp_path / "users"), "TENANTS_FILE": str(tmp_path / "none.json"), **env})


def test_eof_stops_cleanly(tmp_path, upstream):
    """Issue #52: one script opens the chat; end of input stops it and leaves no
    guest running."""
    proc = run_cli(tmp_path, upstream, "--session", "named")
    out, err = proc.communicate("hi\n", timeout=60)
    assert proc.returncode == 0, err
    assert out.splitlines()[:2] == [f"vllm m {upstream.url}", "session: named"]
    assert "hello there" in out
    assert guests(tmp_path) == []


def test_sigint_stops_cleanly(tmp_path, upstream):
    """Issue #52: Ctrl-C stops the harness cleanly and leaves no guest running."""
    proc = run_cli(tmp_path, upstream)
    proc.stdin.write("hi\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 30
    while not (upstream.requests and guests(tmp_path)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert guests(tmp_path), "no guest started"
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert guests(tmp_path) == []


@pytest.mark.parametrize("args, env, named", [
    (["--config", "/nonexistent.json"], {}, "/nonexistent.json"),
    ([], {"LLM_VENDOR": "foo"}, "llm.vendor"),
])
def test_cli_refuses_bad_config(tmp_path, upstream, args, env, named):
    """Issue #52: a missing or invalid config file, or an unknown vendor, is a
    clear error naming it; the script does not start (exit 2, no guest)."""
    proc = run_cli(tmp_path, upstream, *args, **env)
    out, err = proc.communicate("hi\n", timeout=30)
    assert proc.returncode == 2
    assert named in err and "session:" not in out
    assert guests(tmp_path) == [] and upstream.requests == []
