"""protocol.messages — envelope, ack and resend rules."""
import json

import pytest

from protocol import SCHEMA_VERSION
from protocol.messages import (
    ACK_FIELDS, CONTROL_TYPES, KINDS, LIVE_TYPES, Inbox, Outbox, ProtocolError,
    ack, control_message, encode, event_envelope, handshake, live, parse,
    resend_plan,
)

SID = "sess-1"


def ev(seq, type="clock", body=None, sid=SID):
    return event_envelope(sid, seq, "2026-10-07T00:00:00Z", SCHEMA_VERSION, type,
                          body if body is not None else {"n": seq})


def ctl(cseq, type="task", body=None, sid=SID):
    return control_message(sid, cseq, type, body if body is not None else {"n": cseq})


def fill(outbox, n, make):
    """Push n messages minted from the outbox; return them."""
    return [outbox.push(make(outbox.next_number())) for _ in range(n)]


# ---------------------------------------------------------------- envelopes


def test_event_envelope_shape():
    e = ev(1, "session_start", {"user": "u"})
    assert e == {"session_id": SID, "seq": 1, "ts": "2026-10-07T00:00:00Z",
                 "schema": SCHEMA_VERSION, "type": "session_start", "body": {"user": "u"}}


def test_control_message_shape():
    m = control_message(SID, 1, "hello", {"config": {}})
    assert m == {"session_id": SID, "cseq": 1, "type": "hello", "body": {"config": {}}}


def test_control_types_match_plan():
    assert CONTROL_TYPES == {"hello", "task", "follow_up", "steer", "ui_response",
                             "side_result", "park", "abort", "heartbeat"}


def test_live_types_match_plan():
    assert LIVE_TYPES == {"ready", "delta", "log", "bulk_ref"}
    assert KINDS == ("event", "control", "ack", "live")


def test_unknown_control_type_rejected():
    with pytest.raises(ProtocolError, match="unknown control type"):
        control_message(SID, 1, "token", {})
    with pytest.raises(ProtocolError, match="unknown control type"):
        parse({"session_id": SID, "cseq": 1, "type": "token", "body": {}})


def test_unknown_live_type_rejected():
    with pytest.raises(ProtocolError, match="unknown live type"):
        live("token", {})
    with pytest.raises(ProtocolError, match="unknown live type"):
        parse({"type": "token", "body": {}})


def test_envelope_rejects_bad_numbers_and_bodies():
    for bad in (0, -1, True, "1", None):
        with pytest.raises(ProtocolError):
            ev(bad)
        with pytest.raises(ProtocolError):
            ctl(bad)
    with pytest.raises(ProtocolError, match="body"):
        control_message(SID, 1, "task", "text")
    with pytest.raises(ProtocolError, match="session_id"):
        control_message("", 1, "task", {})


def test_event_envelope_schema_mismatch_rejected():
    with pytest.raises(ProtocolError, match="schema mismatch"):
        event_envelope(SID, 1, "t", SCHEMA_VERSION + 1, "clock", {})
    for bad in (SCHEMA_VERSION + 1, float(SCHEMA_VERSION), str(SCHEMA_VERSION), True):
        with pytest.raises(ProtocolError, match="schema mismatch"):
            parse({**ev(1), "schema": bad})


def test_event_ts_must_be_a_non_empty_string_in_both_paths():
    for bad in ("", None, 0, 1.5):
        with pytest.raises(ProtocolError, match="ts must be a non-empty string"):
            event_envelope(SID, 1, bad, SCHEMA_VERSION, "clock", {})
        with pytest.raises(ProtocolError, match="ts must be a non-empty string"):
            parse({**ev(1), "ts": bad})


# ---------------------------------------------------------------- acks


def test_ack_carries_exactly_one_kind():
    assert ack(SID, ack_seq=3) == {"type": "ack", "session_id": SID, "ack_seq": 3}
    assert ack(SID, ack_cseq=0) == {"type": "ack", "session_id": SID, "ack_cseq": 0}
    with pytest.raises(ProtocolError, match="exactly one"):
        ack(SID)
    with pytest.raises(ProtocolError, match="exactly one"):
        ack(SID, ack_seq=1, ack_cseq=1)


def test_parse_rejects_both_ack_kinds_in_one_message():
    with pytest.raises(ProtocolError, match="both"):
        parse({"type": "ack", "session_id": SID, "ack_seq": 1, "ack_cseq": 1})
    with pytest.raises(ProtocolError, match="neither"):
        parse({"type": "ack", "session_id": SID})


def test_ack_value_must_be_non_negative_int():
    for bad in (-1, True, "2", 1.0):
        with pytest.raises(ProtocolError, match="non-negative int"):
            ack(SID, ack_seq=bad)
        with pytest.raises(ProtocolError, match="non-negative int"):
            parse({"type": "ack", "session_id": SID, "ack_cseq": bad})


def test_ack_session_id_checked_in_both_paths():
    with pytest.raises(ProtocolError, match="session_id"):
        ack("", ack_seq=1)
    with pytest.raises(ProtocolError, match="session_id"):
        parse({"type": "ack", "session_id": "", "ack_seq": 1})
    with pytest.raises(ProtocolError, match="missing field: session_id"):
        parse({"type": "ack", "ack_seq": 1})


def test_handshake_is_an_ack_of_cseq():
    h = handshake(SID, 7)
    assert h == ack(SID, ack_cseq=7)
    assert parse(h)[0] == "ack"
    assert parse(handshake(SID, 0))[0] == "ack"  # fresh guest: nothing delivered


# ---------------------------------------------------------------- parse


def test_parse_classifies_all_four_kinds():
    assert parse(ev(1))[0] == "event"
    assert parse(ctl(1))[0] == "control"
    assert parse(ack(SID, ack_seq=1))[0] == "ack"
    assert parse(live("ready", {}))[0] == "live"


def test_parse_accepts_jsonl_lines_and_bytes():
    kind, msg = parse(encode(ev(2)))
    assert kind == "event" and msg == ev(2)
    kind, msg = parse(encode(ctl(2)).encode("utf-8"))
    assert kind == "control" and msg == ctl(2)
    assert encode({"a": "é"}) == '{"a":"é"}\n'
    assert json.loads(encode(live("log", {"m": "x"}))) == live("log", {"m": "x"})


def test_parse_rejects_non_objects_and_missing_fields():
    with pytest.raises(ProtocolError, match="not JSON"):
        parse("{nope")
    with pytest.raises(ProtocolError, match="object"):
        parse([1, 2])
    with pytest.raises(ProtocolError, match="missing field: type"):
        parse({"session_id": SID, "seq": 1})
    for f in ("session_id", "ts", "schema", "body"):
        broken = ev(1)
        del broken[f]
        with pytest.raises(ProtocolError, match=f"missing field: {f}"):
            parse(broken)
    with pytest.raises(ProtocolError, match="missing field: body"):
        parse({"session_id": SID, "cseq": 1, "type": "task"})
    with pytest.raises(ProtocolError, match="without cseq"):
        parse({"session_id": SID, "type": "task", "body": {}})


def test_live_messages_carry_no_seq_and_parse_as_live():
    for t in LIVE_TYPES:
        m = live(t, {"turn_id": "turn_x", "kind": "text", "text": "hi"})
        assert "seq" not in m and "cseq" not in m and "session_id" not in m
        assert parse(m) == ("live", m)


# ---------------------------------------------------------------- Outbox


def test_numbering_starts_at_one_and_is_monotonic():
    ob = Outbox(SID, "seq")
    assert [ob.next_number() for _ in range(4)] == [1, 2, 3, 4]
    assert ob.highest_sent == 0 and ob.highest_acked == 0


def test_push_keeps_unacked_in_order():
    ob = Outbox(SID, "seq")
    sent = fill(ob, 3, ev)
    assert ob.pending == sent
    assert ob.highest_sent == 3
    assert len(ob) == 3


def test_push_without_next_number_advances_the_counter():
    ob = Outbox(SID, "cseq")
    ob.push(ctl(1))
    ob.push(ctl(2))
    assert ob.next_number() == 3


def test_push_rejects_out_of_order_wrong_field_and_foreign_session():
    ob = Outbox(SID, "seq")
    ob.push(ev(1))
    with pytest.raises(ProtocolError, match="out of order"):
        ob.push(ev(3))
    with pytest.raises(ProtocolError, match="out of order"):
        ob.push(ev(1))
    with pytest.raises(ProtocolError, match="missing field: seq"):
        ob.push(ctl(2))
    with pytest.raises(ProtocolError, match="foreign session_id"):
        ob.push(ev(2, sid="other"))


def test_acknowledge_drops_everything_up_to_n():
    ob = Outbox(SID, "seq")
    sent = fill(ob, 5, ev)
    ob.acknowledge(3)
    assert ob.highest_acked == 3
    assert ob.pending == sent[3:]
    ob.acknowledge(5)
    assert ob.pending == []


def test_acknowledge_beyond_highest_sent_is_an_error():
    ob = Outbox(SID, "seq")
    fill(ob, 2, ev)
    with pytest.raises(ProtocolError, match="beyond highest sent"):
        ob.acknowledge(3)
    assert ob.highest_acked == 0 and len(ob) == 2


def test_stale_ack_is_a_no_op():
    ob = Outbox(SID, "seq")
    fill(ob, 4, ev)
    ob.acknowledge(3)
    ob.acknowledge(1)
    assert ob.highest_acked == 3 and len(ob) == 1


def test_resend_from_ack_plus_one_reproduces_the_unacked_tail():
    ob = Outbox(SID, "seq")
    sent = fill(ob, 6, ev)
    ob.acknowledge(2)
    assert ob.pending_after(2) == sent[2:]
    assert [m["seq"] for m in ob.pending_after(4)] == [5, 6]
    assert ob.pending_after(6) == []


def test_seq_and_cseq_are_independent_streams():
    guest = Outbox(SID, "seq")
    host = Outbox(SID, "cseq")
    fill(guest, 3, ev)
    fill(host, 1, ctl)
    guest.acknowledge(3)
    assert host.highest_sent == 1 and host.highest_acked == 0
    assert host.next_number() == 2 and guest.next_number() == 4
    assert "cseq" in host.pending[0] and "seq" not in host.pending[0]


def test_outbox_rejects_unknown_field():
    with pytest.raises(ProtocolError, match="field"):
        Outbox(SID, "ack_seq")
    with pytest.raises(ProtocolError, match="field"):
        Inbox(SID, "n")


# ---------------------------------------------------------------- Inbox


def test_inbox_delivers_in_order_and_acks_highest_contiguous():
    ib = Inbox(SID, "seq")
    assert ib.ack_value() == 0
    assert ib.accept(ev(1)) == "new"
    assert ib.accept(ev(2)) == "new"
    assert ib.last_delivered == 2
    assert ib.ack_value() == 2
    assert ib.ack() == ack(SID, ack_seq=2)
    assert Inbox(SID, "cseq").ack() == ack(SID, ack_cseq=0)
    assert ACK_FIELDS == {"seq": "ack_seq", "cseq": "ack_cseq"}


def test_inbox_drops_duplicates():
    ib = Inbox(SID, "seq")
    ib.accept(ev(1))
    ib.accept(ev(2))
    assert ib.accept(ev(1)) == "duplicate"
    assert ib.accept(ev(2)) == "duplicate"
    assert ib.last_delivered == 2


def test_inbox_reports_a_gap_without_delivering_it():
    ib = Inbox(SID, "seq")
    ib.accept(ev(1))
    assert ib.accept(ev(3)) == "gap"
    assert ib.last_delivered == 1
    assert ib.ack_value() == 1  # the ack does not move past the hole


def test_gap_is_filled_by_the_later_resend():
    ib = Inbox(SID, "seq")
    ib.accept(ev(1))
    assert ib.accept(ev(3)) == "gap"
    assert ib.accept(ev(2)) == "new"
    assert ib.accept(ev(3)) == "new"  # the resend of 3 is new, not a duplicate
    assert ib.ack_value() == 3


def test_inbox_rejects_foreign_session():
    ib = Inbox(SID, "seq")
    with pytest.raises(ProtocolError, match="foreign session_id"):
        ib.accept(ev(1, sid="other"))
    with pytest.raises(ProtocolError, match="foreign session_id"):
        ib.accept({"seq": 1, "body": {}})
    assert ib.last_delivered == 0


def test_inbox_rejects_wrong_field_and_bad_numbers():
    ib = Inbox(SID, "cseq")
    with pytest.raises(ProtocolError, match="missing field: cseq"):
        ib.accept(ev(1))
    with pytest.raises(ProtocolError, match="positive int"):
        ib.accept({"session_id": SID, "cseq": 0, "type": "task", "body": {}})


def test_scrambled_redelivery_still_delivers_in_order():
    """Out-of-order resend: whatever arrives early is a gap, the ack stays at
    the contiguous prefix, and the sender's next resend fills it."""
    ob = Outbox(SID, "seq")
    sent = fill(ob, 5, ev)
    ib = Inbox(SID, "seq")
    ib.accept(sent[0])
    ob.acknowledge(ib.ack_value())

    delivered = [1]
    scrambled = [sent[3], sent[1], sent[4], sent[2], sent[0]]
    results = []
    for m in scrambled:
        r = ib.accept(m)
        results.append(r)
        if r == "new":
            delivered.append(m["seq"])
    assert results == ["gap", "new", "gap", "new", "duplicate"]
    assert delivered == [1, 2, 3]

    # the peer re-acks the contiguous prefix; the resend from ack+1 is in order
    for m in resend_plan(ob, ib.ack_value()):
        assert ib.accept(m) == "new"
        delivered.append(m["seq"])
    assert delivered == [1, 2, 3, 4, 5]
    assert ib.ack_value() == 5
    ob.acknowledge(5)
    assert len(ob) == 0


# ---------------------------------------------------------------- reconnect


def test_reconnect_both_sides_resend_from_ack_plus_one():
    guest_out, host_in = Outbox(SID, "seq"), Inbox(SID, "seq")
    host_out, guest_in = Outbox(SID, "cseq"), Inbox(SID, "cseq")

    # before the drop: host sent hello+task, guest delivered both; guest sent 4
    # events of which the host durably stored and acked only 2
    for m in fill(host_out, 2, ctl):
        guest_in.accept(m)
    host_out.acknowledge(guest_in.ack_value())
    events = fill(guest_out, 4, ev)
    host_in.accept(events[0])
    host_in.accept(events[1])
    guest_out.acknowledge(host_in.ack_value())
    host_out.push(ctl(host_out.next_number(), "steer"))  # lost on the wire

    # new connection: guest's first message is its handshake ack of cseq
    first = handshake(SID, guest_in.ack_value())
    kind, msg = parse(first)
    assert kind == "ack" and msg["ack_cseq"] == 2
    host_resend = resend_plan(host_out, msg["ack_cseq"])
    assert [m["cseq"] for m in host_resend] == [3]

    # host answers with its ack of seq; guest resends from ack+1
    reply = parse(ack(SID, ack_seq=host_in.ack_value()))[1]
    guest_resend = resend_plan(guest_out, reply["ack_seq"])
    assert guest_resend == events[2:]

    for m in guest_resend:
        assert host_in.accept(m) == "new"
    for m in host_resend:
        assert guest_in.accept(m) == "new"
    assert host_in.ack_value() == 4 and guest_in.ack_value() == 3


def test_resend_plan_applies_the_ack_and_rejects_impossible_acks():
    ob = Outbox(SID, "seq")
    fill(ob, 3, ev)
    assert [m["seq"] for m in resend_plan(ob, 1)] == [2, 3]
    assert ob.highest_acked == 1
    with pytest.raises(ProtocolError, match="beyond highest sent"):
        resend_plan(ob, 4)
    with pytest.raises(ProtocolError, match="regressed"):
        resend_plan(ob, 0)
    assert [m["seq"] for m in resend_plan(ob, 1)] == [2, 3]  # same ack again is fine
