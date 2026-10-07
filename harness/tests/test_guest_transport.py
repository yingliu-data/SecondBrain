"""guest.transport over Unix sockets against tests/fakes/fake_host.FakeHost
(docs/PROTOCOL.md sections 3 and 4)."""
import queue
import socket
import time

import pytest

from guest.transport import Transport, UnixConnector, VsockConnector, socket_path
from protocol.events import make_event
from protocol.messages import ProtocolError
from tests.fakes.fake_host import FakeHost

SID = "sess-t"
HELLO = {"session_id": SID, "principal": {"user": "u", "tenant": "t"},
         "config": {"model": "m"}, "system_block": {}, "resume": False, "at": 1}


def ev(seq, type="clock", body=None):
    return make_event(SID, seq, "2026-10-07T00:00:00Z", type,
                      body if body is not None else {"kind": "turn_clock", "at": seq})


def drain(inbound, timeout=0.2) -> list:
    items = []
    while True:
        try:
            items.append(inbound.get(timeout=timeout))
        except queue.Empty:
            return items


def wait_item(inbound, kind, timeout=5.0, **match):
    """The first inbound item of `kind` (and, for controls, matching type)."""
    while True:
        item = inbound.get(timeout=timeout)
        if item[0] != kind:
            continue
        if kind == "control" and match and item[1]["type"] != match.get("type"):
            continue
        if kind == "link" and match and item[1] != match.get("state"):
            continue
        return item


@pytest.fixture
def host(tmp_path):
    with FakeHost(tmp_path, HELLO) as h:
        yield h


@pytest.fixture
def link(tmp_path, host):
    inbound = queue.Queue()
    transport = Transport(UnixConnector(tmp_path), inbound, backoff_initial=0.02)
    transport.start()
    hello = wait_item(inbound, "control", type="hello")
    assert hello[1]["body"] == HELLO
    yield transport, inbound, host
    transport.stop()


# ---------------------------------------------------------------- connectors


def test_unix_connector_path_naming(tmp_path):
    c = UnixConnector(tmp_path)
    assert c.path(5000) == str(tmp_path / "v.sock_5000")
    assert socket_path(tmp_path, 5010) == str(tmp_path / "v.sock_5010")
    with pytest.raises(OSError):
        c.connect(5000)                 # nobody listening


def test_unix_connector_connects(tmp_path, host):
    sock = UnixConnector(tmp_path).connect(5000)
    assert isinstance(sock, socket.socket)
    sock.close()


def test_vsock_connector_defaults_to_host_cid():
    c = VsockConnector()
    assert c.cid == 2
    with pytest.raises(OSError):        # no vsock peer here; still a socket error, not a crash
        c.connect(5000)


# ---------------------------------------------------------------- basics


def test_session_learned_from_hello(link):
    transport, inbound, host = link
    assert transport.session_id == SID
    assert transport.inbox.last_delivered == 1
    host.wait_acked_cseq(1)
    assert host.guest_acked_cseq == 1
    assert transport.connected


def test_events_acked_in_order(link):
    transport, inbound, host = link
    for seq in range(1, 6):
        transport.send_event(ev(seq))
    assert transport.wait_acked(5, timeout=5)
    assert [e["seq"] for e in host.events] == [1, 2, 3, 4, 5]
    assert host.acked_seq == 5
    assert transport.acked_seq == 5
    assert len(transport.outbox) == 0
    acks = [n for kind, n in ((i[0], i[1]) for i in drain(inbound)) if kind == "ack"]
    assert acks == sorted(acks) and acks[-1] == 5
    assert host.errors == []


def test_send_event_out_of_order_rejected(link):
    transport, inbound, host = link
    transport.send_event(ev(1))
    with pytest.raises(ProtocolError, match="out of order"):
        transport.send_event(ev(3))
    with pytest.raises(ProtocolError, match="foreign session"):
        transport.send_event(make_event("other", 2, "ts", "clock", {"kind": "turn_clock", "at": 1}))


def test_live_messages_not_acked(link):
    transport, inbound, host = link
    transport.send_live("ready", {})
    transport.send_live("delta", {"turn_id": "t1", "kind": "text", "text": "hi"})
    host.wait_live("delta")
    assert host.wait_live("ready") == {"type": "ready", "body": {}}
    assert host.events == []
    assert host.acked_seq == 0
    assert [i for i in drain(inbound) if i[0] == "ack"] == []
    with pytest.raises(ProtocolError, match="unknown live type"):
        transport.send_live("token", {})


def test_wait_acked_times_out_while_acks_paused(link):
    transport, inbound, host = link
    host.pause_acks()
    transport.send_event(ev(1))
    host.wait_for_seq(1)
    assert transport.wait_acked(1, timeout=0.2) is False
    assert len(transport.outbox) == 1
    host.resume_acks()
    assert transport.wait_acked(1, timeout=5)
    assert len(transport.outbox) == 0


def test_controls_delivered_and_acked(link):
    transport, inbound, host = link
    host.send("task", {"input_id": "in-1", "text": "hi", "attachments": []})
    host.send("heartbeat", {"at": 2})
    task = wait_item(inbound, "control", type="task")
    beat = wait_item(inbound, "control", type="heartbeat")
    assert task[1]["cseq"] == 2 and beat[1]["cseq"] == 3
    host.wait_acked_cseq(3)
    assert transport.last_delivered_cseq == 3
    transport.send_ack(3)               # explicit ack is harmless
    host.wait_acked_cseq(3)


def test_control_redelivered_twice_reaches_inbound_once(link):
    transport, inbound, host = link
    msg = host.send("task", {"input_id": "in-1", "text": "hi", "attachments": []})
    wait_item(inbound, "control", type="task")
    host.wait_acked_cseq(2)
    host.redeliver(msg)
    host.redeliver(msg)
    # three acks of cseq 2: one per delivery (the duplicate ones re-ack)
    for _ in range(100):
        if host.acks.count(2) >= 3:
            break
        time.sleep(0.02)
    assert host.acks.count(2) == 3
    assert [i for i in drain(inbound) if i[0] == "control"] == []


def test_control_gap_waits_for_resend(link):
    transport, inbound, host = link
    held = host.send("task", {"input_id": "in-1", "text": "first", "attachments": []},
                     deliver=False)                       # cseq 2 not on the wire
    ahead = host.send("task", {"input_id": "in-2", "text": "second", "attachments": []})  # cseq 3
    assert drain(inbound, 0.2) == []                      # the gap is not delivered
    assert transport.last_delivered_cseq == 1
    host.redeliver(held)
    first = wait_item(inbound, "control", type="task")
    assert first[1]["cseq"] == 2
    host.redeliver(ahead)
    second = wait_item(inbound, "control", type="task")
    assert second[1]["cseq"] == 3
    host.wait_acked_cseq(3)


def test_garbage_lines_are_skipped(link):
    transport, inbound, host = link
    host.write_raw("not json\n\n{\"type\": \"ack\"}\n")
    host.write_raw('{"session_id":"other","cseq":2,"type":"task","body":{}}\n')
    host.send("heartbeat", {"at": 1})
    beat = wait_item(inbound, "control", type="heartbeat")
    assert beat[1]["cseq"] == 2
    assert transport.connected


# ---------------------------------------------------------------- reconnect


def test_link_drop_reconnects_without_duplicates_or_gaps(link):
    transport, inbound, host = link
    for seq in range(1, 4):
        transport.send_event(ev(seq))
    assert transport.wait_acked(3, timeout=5)

    host.pause_acks()
    transport.send_event(ev(4))
    transport.send_event(ev(5))
    host.wait_for_seq(5)                 # on the wire, unacked
    host.drop_link()
    assert wait_item(inbound, "link", state="down")
    transport.send_event(ev(6))          # while down: held in the outbox

    host.wait_connected(count=2)
    host.resume_acks()
    assert wait_item(inbound, "link", state="up")
    transport.send_event(ev(7))
    assert transport.wait_acked(7, timeout=5)

    seqs = [e["seq"] for e in host.events]
    assert seqs == [1, 2, 3, 4, 5, 6, 7]
    raw = [e["seq"] for e in host.raw_events]
    assert raw == sorted(raw)
    assert host.handshakes == [1]        # the ack of cseq 1 (hello) opened the reconnect
    assert host.errors == []
    assert transport.connections == 2


def test_resend_only_what_the_host_lacks(link):
    transport, inbound, host = link
    for seq in range(1, 4):
        transport.send_event(ev(seq))
    assert transport.wait_acked(3, timeout=5)
    host.pause_acks()
    transport.send_event(ev(4))
    host.wait_for_seq(4)
    host.drop_link()
    host.wait_connected(count=2)
    host.resume_acks()                   # the host answers the handshake with ack 4
    transport.send_event(ev(5))
    assert transport.wait_acked(5, timeout=5)
    assert [e["seq"] for e in host.raw_events] == [1, 2, 3, 4, 5]   # nothing resent
    assert [e["seq"] for e in host.events] == [1, 2, 3, 4, 5]


def test_events_sent_while_down_arrive_after_reconnect(link):
    transport, inbound, host = link
    host.drop_link()
    wait_item(inbound, "link", state="down")
    for seq in range(1, 4):
        transport.send_event(ev(seq))
    host.wait_connected(count=2)
    assert transport.wait_acked(3, timeout=5)
    assert [e["seq"] for e in host.events] == [1, 2, 3]


def test_handshake_resends_controls(link):
    transport, inbound, host = link
    host.drop_link()
    wait_item(inbound, "link", state="down")
    pending = host.send("task", {"input_id": "in-1", "text": "hi", "attachments": []})
    assert pending["cseq"] == 2
    task = wait_item(inbound, "control", type="task")
    assert task[1] == pending
    host.wait_acked_cseq(2)
    assert host.handshakes == [1]
    assert [i for i in drain(inbound) if i[0] == "control"] == []   # exactly once


def test_control_in_flight_across_drop_delivered_once(link):
    transport, inbound, host = link
    first = host.send("task", {"input_id": "in-1", "text": "one", "attachments": []})
    wait_item(inbound, "control", type="task")
    host.wait_acked_cseq(2)
    host.drop_link()
    wait_item(inbound, "link", state="down")
    host.wait_connected(count=2)
    wait_item(inbound, "link", state="up")
    host.redeliver(first)                # a stale duplicate after the reconnect
    second = host.send("task", {"input_id": "in-2", "text": "two", "attachments": []})
    got = wait_item(inbound, "control", type="task")
    assert got[1] == second
    assert [i for i in drain(inbound) if i[0] == "control"] == []


def test_multiple_drops(link):
    transport, inbound, host = link
    seq = 0
    for round_ in range(3):
        for _ in range(2):
            seq += 1
            transport.send_event(ev(seq))
        host.wait_for_seq(seq)
        host.drop_link()
        wait_item(inbound, "link", state="down")
        host.wait_connected(count=round_ + 2)
        wait_item(inbound, "link", state="up")
    seq += 1
    transport.send_event(ev(seq))
    assert transport.wait_acked(seq, timeout=5)
    assert [e["seq"] for e in host.events] == list(range(1, seq + 1))
    assert host.errors == []


def test_connects_once_host_appears(tmp_path):
    inbound = queue.Queue()
    transport = Transport(UnixConnector(tmp_path), inbound, backoff_initial=0.02,
                          backoff_max=0.1)
    transport.start()
    assert drain(inbound, 0.1) == []
    with FakeHost(tmp_path, HELLO) as host:
        hello = wait_item(inbound, "control", type="hello")
        assert hello[1]["cseq"] == 1
        transport.send_event(ev(1))
        assert transport.wait_acked(1, timeout=5)
        assert host.events[0]["seq"] == 1
    transport.stop()


def test_known_session_handshakes_on_first_connection(tmp_path):
    """A guest that already knows its session (resume) opens with a handshake
    and the host answers it; the host's ack carries over."""
    inbound = queue.Queue()
    with FakeHost(tmp_path, HELLO, auto_hello=False) as host:
        transport = Transport(UnixConnector(tmp_path), inbound, session_id=SID,
                              backoff_initial=0.02)
        transport.start()
        wait_item(inbound, "link", state="up")
        assert host.handshakes == [0]
        transport.send_event(ev(1))
        assert transport.wait_acked(1, timeout=5)
        transport.stop()


def test_stop_ends_threads(link):
    transport, inbound, host = link
    transport.stop()
    assert not transport._link_thread.is_alive()
    assert not transport._writer_thread.is_alive()
    host.wait_disconnected()
    with pytest.raises(RuntimeError):
        transport.start()


def test_side_result_from_fake_host(link):
    transport, inbound, host = link
    host.side_results["call-1"] = {"content": "sunny", "ok": True}
    transport.send_event(ev(1, "tool_call", {
        "turn_id": "turn-1", "call_id": "call-1", "name": "weather", "side": "host",
        "args": {}, "state": "RUNNING"}))
    result = wait_item(inbound, "control", type="side_result")
    assert result[1]["body"] == {"call_id": "call-1", "side": "host", "content": "sunny",
                                 "ok": True}
    transport.send_event(ev(2, "tool_call", {
        "turn_id": "turn-1", "call_id": "call-2", "name": "bash", "side": "guest",
        "args": {}, "state": "RUNNING"}))
    assert transport.wait_acked(2, timeout=5)
    assert [i for i in drain(inbound) if i[0] == "control"] == []   # guest side: no answer


def test_fake_host_callable_side_results(tmp_path):
    inbound = queue.Queue()
    with FakeHost(tmp_path, HELLO, side_results=lambda call: {"content": call["name"], "ok": True}) as host:
        transport = Transport(UnixConnector(tmp_path), inbound, backoff_initial=0.02)
        transport.start()
        wait_item(inbound, "control", type="hello")
        transport.send_event(ev(1, "tool_call", {
            "turn_id": "turn-1", "call_id": "call-9", "name": "calendar", "side": "device",
            "args": {}, "state": "RUNNING"}))
        result = wait_item(inbound, "control", type="side_result")
        assert result[1]["body"]["content"] == "calendar"
        transport.stop()
