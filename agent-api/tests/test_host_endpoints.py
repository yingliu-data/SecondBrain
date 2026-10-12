"""Endpoints: listeners named after the vsock ports; ControlChannel hands
parsed lines to the asyncio loop."""
import asyncio
import os
import socket
import stat
import threading
import time

import pytest

from protocol.messages import encode

from app.host.endpoints import ControlChannel, Endpoints, socket_path


def test_listeners_are_created_unlinked_and_chmod_600(tmp_path):
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    stale = ipc / "v.sock_5000"
    stale.write_text("stale")
    ep = Endpoints(tmp_path, {"control": 5000, "bulk": 5001, "llm": 5010})
    for port in (5000, 5001, 5010):
        p = socket_path(tmp_path, port)
        assert p.exists() and stat.S_ISSOCK(p.stat().st_mode)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert ep.uds_path == ipc / "v.sock"
    assert ep.accept_control(timeout=0.05) is None
    ep.close()
    assert not socket_path(tmp_path, 5000).exists()


@pytest.mark.asyncio
async def test_accept_control_and_channel_round_trip(tmp_path):
    ep = Endpoints(tmp_path, [5000])
    loop = asyncio.get_running_loop()
    inbound: asyncio.Queue = asyncio.Queue()
    accepted: list = []

    def acceptor():
        accepted.append(ep.accept_control(timeout=5))

    t = threading.Thread(target=acceptor)
    t.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(ep.path(5000)))
    t.join(5)
    assert accepted and accepted[0] is not None
    ch = ControlChannel(accepted[0], loop, inbound)
    ev = {"session_id": "s", "seq": 1, "ts": "t", "schema": 1, "type": "x", "body": {}}
    client.sendall(encode(ev).encode() + b"not json\n" + encode({"type": "ack", "session_id": "s",
                                                                  "ack_cseq": 0}).encode())
    kind, msg = await asyncio.wait_for(inbound.get(), 5)
    assert kind == "event" and msg["seq"] == 1
    kind, msg = await asyncio.wait_for(inbound.get(), 5)   # the bad line was dropped
    assert kind == "ack" and msg["ack_cseq"] == 0
    ch.send({"session_id": "s", "cseq": 1, "type": "heartbeat", "body": {"at": 1}})
    assert b'"heartbeat"' in client.recv(4096)
    client.close()
    item = await asyncio.wait_for(inbound.get(), 5)
    assert item[:2] == ("link", "down") and item[2] is ch
    assert ch.closed
    ep.close()


# ---------------------------------------------------- accept wait (restore path)


def _accept_in_thread(ep, timeout):
    out: dict = {}

    def run():
        t0 = time.monotonic()
        out["conn"] = ep.accept_control(timeout=timeout)
        out["s"] = time.monotonic() - t0

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _connect(tmp_path, port=5000):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(socket_path(tmp_path, port)))
    return c


def test_reopen_wakes_a_parked_accept_at_once(tmp_path):
    """While :5000 is closed accept_control waits on the Condition; reopen()
    + the guest's connect is accepted at once, not after a polling sleep."""
    ep = Endpoints(tmp_path, [5000])
    ep.close_port(5000)
    t, out = _accept_in_thread(ep, timeout=10)
    time.sleep(0.1)
    assert t.is_alive()                       # waiting, no listener
    t0 = time.monotonic()
    ep.reopen(5000)
    c = _connect(tmp_path)
    t.join(5)
    assert out["conn"] is not None
    assert time.monotonic() - t0 < 0.2        # was up to 0.5 s (sleep while closed)
    out["conn"].close(); c.close(); ep.close()


def test_close_port_during_accept_then_reopen(tmp_path):
    """A close_port while accept polls the listener wakes it; it waits for
    the reopened listener and accepts on it."""
    ep = Endpoints(tmp_path, [5000])
    t, out = _accept_in_thread(ep, timeout=10)
    time.sleep(0.05)
    ep.close_port(5000)
    assert not socket_path(tmp_path, 5000).exists()
    time.sleep(0.05)
    assert t.is_alive()
    ep.reopen(5000)
    c = _connect(tmp_path)
    t.join(5)
    assert out["conn"] is not None and out["s"] < 5
    out["conn"].close(); c.close(); ep.close()


def test_closed_port_times_out_without_polling(tmp_path):
    ep = Endpoints(tmp_path, [5000])
    ep.close_port(5000)
    t0 = time.monotonic()
    assert ep.accept_control(timeout=0.2) is None
    assert 0.15 <= time.monotonic() - t0 < 1.0
    assert ep.accept_control(timeout=0) is None
    ep.close()


@pytest.mark.parametrize("parked", [True, False])
def test_close_wakes_an_unbounded_accept(tmp_path, parked):
    ep = Endpoints(tmp_path, [5000])
    if parked:
        ep.close_port(5000)
    t, out = _accept_in_thread(ep, timeout=None)
    time.sleep(0.05)
    ep.close()
    t.join(2)
    assert not t.is_alive() and out["conn"] is None
    assert ep.accept_control(timeout=None) is None   # after close: at once


def test_reopen_is_a_noop_when_open_and_returns_the_listener(tmp_path):
    ep = Endpoints(tmp_path, [5000, 5001])
    lst = ep.listener(5001)
    assert ep.reopen(5001) is lst
    ep.close_port(5001)
    new = ep.reopen(5001)
    assert new is not lst and ep.listener(5001) is new
    ep.close()
