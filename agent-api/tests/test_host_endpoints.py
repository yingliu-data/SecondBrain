"""Endpoints: listeners named after the vsock ports; ControlChannel hands
parsed lines to the asyncio loop."""
import asyncio
import os
import socket
import stat
import threading

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
