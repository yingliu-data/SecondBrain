"""guest.main — arguments, idle-wait and the hello phase against FakeHost."""
import queue
import threading
import time

import pytest

from guest import main as guest_main
from guest.config import GuestConfig
from guest.main import (
    EXIT_HELLO_TIMEOUT, EXIT_PROTOCOL, FatalProtocolError, HelloTimeout,
    make_connector, parse_args, run_until_hello,
)
from guest.eventlog import EventLog
from guest.transport import Transport, UnixConnector, VsockConnector
from protocol.fold import replay
from tests.fakes.fake_host import FakeHost
from tests.fakes.fake_llm import FakeLLM, text

SID = "sess-main"
HELLO = {"session_id": SID, "principal": {"user": "u", "tenant": "t"},
         "config": {"model": "m", "tools": [{"name": "calendar", "side": "device", "pin": None}]},
         "system_block": {"base_prompt": "x"}, "resume": False, "at": 7}


def args(tmp_path, *extra):
    return ["--connect", f"unix:{tmp_path}", "--log-dir", str(tmp_path / "log"),
            "--workspace", str(tmp_path / "ws"), *extra]


# ---------------------------------------------------------------- arguments


def test_parse_args_and_connectors(tmp_path):
    ns = parse_args(args(tmp_path, "--idle-wait", "vsock", "--hello-timeout", "2"))
    assert ns.connect == f"unix:{tmp_path}"
    assert ns.idle_wait == "vsock"
    assert parse_args(args(tmp_path)).idle_wait is None
    assert ns.hello_timeout == 2.0
    assert ns.control_port == 5000
    assert isinstance(make_connector(f"unix:{tmp_path}"), UnixConnector)
    assert make_connector("vsock").cid == 2
    assert isinstance(make_connector("vsock:3"), VsockConnector)
    assert make_connector("vsock:3").cid == 3
    for bad in ("tcp:1", "unix:", "vsock:x"):
        with pytest.raises(ValueError):
            make_connector(bad)
    with pytest.raises(SystemExit):
        parse_args(["--connect", "vsock"])          # --log-dir and --workspace required
    with pytest.raises(SystemExit):
        parse_args(args(tmp_path, "--idle-wait", "/tmp/go"))   # the M1 file form is gone


# ---------------------------------------------------------------- hello phase


def test_run_until_hello_with_fake_host(tmp_path):
    inbound = queue.Queue()
    with FakeHost(tmp_path, HELLO) as host:
        transport = Transport(UnixConnector(tmp_path), inbound, backoff_initial=0.02)
        transport.start()
        try:
            cfg = run_until_hello(inbound, timeout=5)
            assert isinstance(cfg, GuestConfig)
            assert cfg.session_id == SID
            assert cfg.at == 7
            assert {"name": "calendar", "side": "device", "pin": None} in cfg.tools
            assert transport.session_id == SID
            host.wait_acked_cseq(1)
        finally:
            transport.stop()


def test_run_until_hello_skips_link_and_ack_items():
    inbound = queue.Queue()
    inbound.put(("link", "up"))
    inbound.put(("ack", 0))
    inbound.put(("control", {"session_id": SID, "cseq": 1, "type": "hello", "body": HELLO}))
    cfg = run_until_hello(inbound, timeout=1)
    assert cfg.session_id == SID


def test_run_until_hello_timeout():
    inbound = queue.Queue()
    inbound.put(("link", "up"))
    t0 = time.monotonic()
    with pytest.raises(HelloTimeout):
        run_until_hello(inbound, timeout=0.1)
    assert 0.05 <= time.monotonic() - t0 < 2


def test_run_until_hello_first_control_not_hello():
    inbound = queue.Queue()
    inbound.put(("control", {"session_id": SID, "cseq": 1, "type": "task",
                             "body": {"input_id": "i", "text": "x", "attachments": []}}))
    with pytest.raises(FatalProtocolError, match="first control message is 'task'"):
        run_until_hello(inbound, timeout=1)


def test_run_until_hello_unusable_hello():
    inbound = queue.Queue()
    inbound.put(("control", {"session_id": SID, "cseq": 1, "type": "hello",
                             "body": {"session_id": SID, "config": {"model": "m"}}}))
    with pytest.raises(FatalProtocolError, match="unusable hello: hello.principal"):
        run_until_hello(inbound, timeout=1)


# ---------------------------------------------------------------- main() exit codes


def test_main_exit_3_on_hello_timeout(tmp_path):
    # nobody listens in tmp_path: the transport keeps retrying, main gives up
    code = guest_main.main(args(tmp_path, "--hello-timeout", "0.3"))
    assert code == EXIT_HELLO_TIMEOUT


def test_main_exit_2_when_first_control_is_not_hello(tmp_path):
    with FakeHost(tmp_path, HELLO, auto_hello=False) as host:
        def send_task():
            host.wait_connected(timeout=5)
            host.send("task", {"input_id": "i", "text": "x", "attachments": []})
        threading.Thread(target=send_task, daemon=True).start()
        code = guest_main.main(args(tmp_path, "--hello-timeout", "5"))
    assert code == EXIT_PROTOCOL


def test_main_exit_2_on_bad_connect_spec(tmp_path):
    assert guest_main.main(["--connect", "tcp:1", "--log-dir", "x", "--workspace", "y"]) == EXIT_PROTOCOL


def test_main_hands_hello_config_to_run_session(tmp_path, monkeypatch):
    seen = {}

    def fake_run_session(cfg, inbound, transport, log_dir, workspace, image_version="dev", **kw):
        seen.update(cfg=cfg, log_dir=log_dir, workspace=workspace,
                    connected=transport.connected, session=transport.session_id)
        return 0

    monkeypatch.setattr(guest_main, "run_session", fake_run_session)
    with FakeHost(tmp_path, HELLO) as host:
        code = guest_main.main(args(tmp_path, "--idle-wait", "vsock", "--hello-timeout", "5"))
        host.wait_disconnected()
    assert code == 0
    assert seen["cfg"].session_id == SID
    assert seen["session"] == SID
    assert seen["connected"] is True
    assert seen["log_dir"] == str(tmp_path / "log")
    assert seen["workspace"] == str(tmp_path / "ws")
    assert host.connections == 1


# ---------------------------------------------------------------- end to end


def test_main_runs_a_text_answer_to_settle(tmp_path):
    """hello -> session_start -> task -> one turn on the fake LLM -> settle;
    the guest stays up until the host aborts, then exits 0; the host's copy of the log replays clean and the local log knows
    the host acked everything."""
    hello = dict(HELLO, config={"model": "fake", "limits": {"max_turns": 2}})
    with FakeHost(tmp_path, hello) as host, FakeLLM(str(tmp_path), [text("Hello there.")]) as llm:
        def send_task():
            host.wait_for("session_start", timeout=10)
            host.send("task", {"input_id": "in-1", "text": "say hi", "attachments": []})
            host.wait_for("settle", timeout=10)
            host.send("abort", {"reason": "test over"})
        threading.Thread(target=send_task, daemon=True).start()
        code = guest_main.main(args(tmp_path, "--hello-timeout", "5"))
        host.wait_disconnected()
    assert code == 0
    types = [e["type"] for e in host.events]
    assert types[0] == "session_start" and types[-1] == "settle"
    assert "input" in types and "turn_start" in types and "llm_response" in types
    assert host.events[-1]["body"]["outcome"] == "done"
    assert host.events[-1]["body"]["answer"] == "Hello there."
    assert len(llm.requests) == 1
    assert host.errors == []
    state, problems = replay(host.events)
    assert problems == []
    assert state.last_settle["input_id"] == "in-1"
    local = EventLog(tmp_path / "log")
    assert local.last_seq == len(host.events)
    assert local.acked_seq == local.last_seq
    assert [e["seq"] for e in local.read()] == [e["seq"] for e in host.events]
