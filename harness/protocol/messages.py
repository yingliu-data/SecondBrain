"""messages — vsock control-channel envelopes and ack/resend bookkeeping
(PLAN.md sections 4.2–4.5, docs/PROTOCOL.md).

Pure data structures: no sockets, no threads, no clock, no randomness. The
guest's `guest/transport` reads and writes the lines; the host's vsock server
does the same on its side. Both lean on this module for what a line means.

Four kinds of line travel on the :5000 control channel (JSONL):

  event    guest -> host   {"session_id","seq","ts","schema","type","body"}
                           the events themselves are the messages
  control  host -> guest   {"session_id","cseq","type","body"}
  ack      either way      {"type":"ack","session_id","ack_seq"} or
                           {"type":"ack","session_id","ack_cseq"}   (exactly one)
  live     guest -> host   {"type","body"}   unsequenced, never stored

Each kind has one checker (_check_event, _check_control, _check_ack,
_check_live) that validates a dict and returns it. The builders assemble a
dict and run it through the checker; parse() classifies a line by shape and
runs the same checker, so a built message and a parsed one obey one rule.

Public names:
  CONTROL_TYPES, LIVE_TYPES, KINDS, ProtocolError
  event_envelope, control_message, ack, live, handshake, parse, encode
  Outbox, Inbox, resend_plan
"""
from __future__ import annotations

import json

from protocol import SCHEMA_VERSION
from protocol.canonical import is_int

CONTROL_TYPES = frozenset({
    "hello", "task", "follow_up", "steer", "ui_response", "side_result",
    "park", "abort", "heartbeat",
})
LIVE_TYPES = frozenset({"ready", "delta", "log", "bulk_ref"})
KINDS = ("event", "control", "ack", "live")

SEQ_FIELDS = ("seq", "cseq")
ACK_FIELDS = {"seq": "ack_seq", "cseq": "ack_cseq"}

_EVENT_FIELDS = ("session_id", "seq", "ts", "schema", "type", "body")
_CONTROL_FIELDS = ("session_id", "cseq", "type", "body")


class ProtocolError(ValueError):
    """A message or a sequence of messages breaks the contract."""


# --------------------------------------------------------------------------
# field checks


def _require(msg: dict, field: str) -> None:
    if field not in msg:
        raise ProtocolError(f"missing field: {field}")


def _check_session_id(session_id) -> str:
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(f"session_id must be a non-empty string, got {session_id!r}")
    return session_id


def _check_number(field: str, n) -> int:
    if not is_int(n, 1):
        raise ProtocolError(f"{field} must be a positive int, got {n!r}")
    return n


def _check_ack_value(field: str, n) -> int:
    # 0 is legal: "nothing delivered yet" (the guest's first handshake).
    if not is_int(n, 0):
        raise ProtocolError(f"{field} must be a non-negative int, got {n!r}")
    return n


def _check_body(body) -> dict:
    if not isinstance(body, dict):
        raise ProtocolError(f"body must be an object, got {type(body).__name__}")
    return body


def _check_field(field: str) -> str:
    if field not in SEQ_FIELDS:
        raise ProtocolError(f"field must be one of {SEQ_FIELDS}, got {field!r}")
    return field


# --------------------------------------------------------------------------
# one checker per kind: validate a dict, return it


def _check_event(msg: dict) -> dict:
    for f in _EVENT_FIELDS:
        _require(msg, f)
    _check_session_id(msg["session_id"])
    _check_number("seq", msg["seq"])
    ts = msg["ts"]
    if not isinstance(ts, str) or not ts:
        raise ProtocolError(f"ts must be a non-empty string, got {ts!r}")
    schema = msg["schema"]
    if not is_int(schema) or schema != SCHEMA_VERSION:
        raise ProtocolError(f"schema mismatch: {schema!r} != {SCHEMA_VERSION}")
    type = msg["type"]
    if not isinstance(type, str) or not type:
        raise ProtocolError(f"event type must be a non-empty string, got {type!r}")
    _check_body(msg["body"])
    return msg


def _check_control(msg: dict) -> dict:
    for f in _CONTROL_FIELDS:
        _require(msg, f)
    _check_session_id(msg["session_id"])
    _check_number("cseq", msg["cseq"])
    if msg["type"] not in CONTROL_TYPES:
        raise ProtocolError(f"unknown control type {msg['type']!r}")
    _check_body(msg["body"])
    return msg


def _check_ack(msg: dict) -> dict:
    _require(msg, "session_id")
    _check_session_id(msg["session_id"])
    has_seq, has_cseq = "ack_seq" in msg, "ack_cseq" in msg
    if has_seq and has_cseq:
        raise ProtocolError("an ack carries exactly one of ack_seq, ack_cseq: got both")
    if not (has_seq or has_cseq):
        raise ProtocolError("an ack carries exactly one of ack_seq, ack_cseq: got neither")
    field = "ack_seq" if has_seq else "ack_cseq"
    _check_ack_value(field, msg[field])
    return msg


def _check_live(msg: dict) -> dict:
    _require(msg, "type")
    if msg["type"] not in LIVE_TYPES:
        raise ProtocolError(f"unknown live type {msg['type']!r}")
    _require(msg, "body")
    _check_body(msg["body"])
    return msg


# --------------------------------------------------------------------------
# builders


def event_envelope(session_id: str, seq: int, ts: str, schema: int, type: str, body: dict) -> dict:
    """Guest -> host. The event is the message; `seq` is the session's event
    sequence. `ts` is data (never a fold input). `schema` must be the current
    SCHEMA_VERSION."""
    return _check_event({"session_id": session_id, "seq": seq, "ts": ts,
                         "schema": schema, "type": type, "body": body})


def control_message(session_id: str, cseq: int, type: str, body: dict) -> dict:
    """Host -> guest. `cseq` is the session's control sequence."""
    return _check_control({"session_id": session_id, "cseq": cseq, "type": type, "body": body})


def ack(session_id: str, ack_seq: int | None = None, ack_cseq: int | None = None) -> dict:
    """Either way. Carries the highest contiguous number delivered: the host
    acks `seq` (after the state sync has appended the event), the guest acks
    `cseq`. Exactly one of the two."""
    msg = {"type": "ack", "session_id": session_id}
    if ack_seq is not None:
        msg["ack_seq"] = ack_seq
    if ack_cseq is not None:
        msg["ack_cseq"] = ack_cseq
    return _check_ack(msg)


def live(type: str, body: dict) -> dict:
    """Guest -> host, unsequenced, never stored: ready, delta, log, bulk_ref."""
    return _check_live({"type": type, "body": body})


def handshake(session_id: str, last_ack_cseq: int) -> dict:
    """The guest's first message on a new control connection (PLAN 4.5): its
    ack of the highest contiguous cseq it has delivered. The host answers
    with its own ack of `seq` and both sides resend from ack + 1."""
    return ack(session_id, ack_cseq=last_ack_cseq)


# --------------------------------------------------------------------------
# wire


def encode(msg: dict) -> str:
    """One JSONL line (with the trailing newline) for a message."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"


def parse(line) -> tuple[str, dict]:
    """Classify one line as ("event" | "control" | "ack" | "live", msg).
    `line` is a decoded JSON object, or a str/bytes JSONL line. Raises
    ProtocolError with a reason for anything that does not fit."""
    msg = line
    if isinstance(msg, (str, bytes, bytearray)):
        try:
            msg = json.loads(msg)
        except ValueError as e:
            raise ProtocolError(f"not JSON: {e}") from None
    if not isinstance(msg, dict):
        raise ProtocolError(f"message must be a JSON object, got {type(msg).__name__}")

    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError("missing field: type")

    if mtype == "ack":
        return "ack", _check_ack(msg)
    if "seq" in msg:
        return "event", _check_event(msg)
    if "cseq" in msg:
        return "control", _check_control(msg)
    if mtype in LIVE_TYPES:
        return "live", _check_live(msg)
    if mtype in CONTROL_TYPES:
        raise ProtocolError(f"control message {mtype!r} without cseq")
    raise ProtocolError(f"unknown live type {mtype!r}")


# --------------------------------------------------------------------------
# bookkeeping


def _number(msg, session_id: str, field: str, who: str) -> int:
    """The sequence number of a message entering an Outbox or Inbox (`who`):
    it must be an object of this session carrying a valid `field`."""
    if not isinstance(msg, dict):
        raise ProtocolError("message must be an object")
    if msg.get("session_id") != session_id:
        raise ProtocolError(
            f"foreign session_id {msg.get('session_id')!r} ({who} is {session_id!r})")
    _require(msg, field)
    return _check_number(field, msg[field])


class Outbox:
    """The sender's side of one sequenced stream: the guest's events
    (field="seq") or the host's control messages (field="cseq").

    Numbers are minted from 1 and never reused; they belong to the session
    and survive reconnects and VM restores. A pushed message stays here
    until the peer acks it; everything after the peer's ack is the resend
    list after a reconnect (PLAN 4.5)."""

    def __init__(self, session_id: str, field: str = "seq"):
        self.session_id = _check_session_id(session_id)
        self.field = _check_field(field)
        self._reserved = 0      # highest number handed out by next_number()
        self.highest_sent = 0   # highest number pushed
        self.highest_acked = 0  # highest number the peer has acked
        self._pending: list[dict] = []

    def next_number(self) -> int:
        """Mint the next number: 1, 2, 3, ... Monotonic; never reused."""
        self._reserved += 1
        return self._reserved

    def push(self, msg: dict) -> dict:
        """Record a sent message. It must be the next one in order
        (highest_sent + 1) and belong to this session."""
        n = _number(msg, self.session_id, self.field, "outbox")
        if n != self.highest_sent + 1:
            raise ProtocolError(
                f"{self.field} {n} out of order: expected {self.highest_sent + 1}")
        self.highest_sent = n
        if n > self._reserved:
            self._reserved = n
        self._pending.append(msg)
        return msg

    def acknowledge(self, n: int) -> None:
        """The peer's ack: highest contiguous number it has delivered. Drops
        every pending message <= n. Acking beyond highest_sent is an error;
        a stale (lower) ack is a no-op."""
        _check_ack_value(self.field, n)
        if n > self.highest_sent:
            raise ProtocolError(
                f"ack {n} beyond highest sent {self.field} {self.highest_sent}")
        if n <= self.highest_acked:
            return
        self.highest_acked = n
        self._pending = [m for m in self._pending if m[self.field] > n]

    def pending_after(self, n: int) -> list[dict]:
        """The unacked messages numbered > n, in order: what to resend after
        a reconnect on which the peer's ack is n."""
        _check_ack_value(self.field, n)
        return [m for m in self._pending if m[self.field] > n]

    def resume_from(self, n: int) -> None:
        """Continue a stream whose messages 1..n the peer already holds (a
        fresh process taking over a session after a log return, PROTOCOL.md
        section 6): the next message is n + 1. Only on an unused Outbox."""
        _check_ack_value(self.field, n)
        if self.highest_sent or self._pending:
            raise ProtocolError(f"resume_from({n}) on an outbox that already sent "
                                f"{self.field} {self.highest_sent}")
        self._reserved = self.highest_sent = self.highest_acked = n

    @property
    def pending(self) -> list[dict]:
        return list(self._pending)

    def __len__(self) -> int:
        return len(self._pending)


class Inbox:
    """The receiver's side of one sequenced stream. Delivers in order:
    accept() says "new" for the next expected number, "duplicate" for a
    number already delivered (dropped), and "gap" for a number further
    ahead (not delivered; the sender's resend will fill it). The ack to
    send is always `last_delivered`, the highest contiguous number."""

    def __init__(self, session_id: str, field: str = "seq"):
        self.session_id = _check_session_id(session_id)
        self.field = _check_field(field)
        self.last_delivered = 0

    def accept(self, msg: dict) -> str:
        n = _number(msg, self.session_id, self.field, "inbox")
        if n <= self.last_delivered:
            return "duplicate"
        if n == self.last_delivered + 1:
            self.last_delivered = n
            return "new"
        return "gap"

    def resume_from(self, n: int) -> None:
        """Treat 1..n as delivered: the next expected number is n + 1. A
        fresh guest process learns its cseq floor from the resume hello's own
        cseq (docs/M3-CONTRACTS.md section 4). Only before anything was
        delivered."""
        _check_ack_value(self.field, n)
        if self.last_delivered:
            raise ProtocolError(f"resume_from({n}) after {self.field} "
                                f"{self.last_delivered} was delivered")
        self.last_delivered = n

    def ack_value(self) -> int:
        """Alias of `last_delivered`: the value to put in the ack."""
        return self.last_delivered

    def ack(self) -> dict:
        """The ack message for this stream."""
        return ack(self.session_id, **{ACK_FIELDS[self.field]: self.last_delivered})


def resend_plan(outbox: Outbox, peer_ack: int) -> list[dict]:
    """After a reconnect: apply the peer's ack and return what to resend,
    in order. The peer may not ack beyond what was sent, nor regress below
    an earlier ack (acked messages are durable at the peer and no longer
    held here)."""
    _check_ack_value(outbox.field, peer_ack)
    if peer_ack < outbox.highest_acked:
        raise ProtocolError(
            f"peer ack {peer_ack} regressed below highest acked {outbox.field} "
            f"{outbox.highest_acked}; those messages are no longer held")
    outbox.acknowledge(peer_ack)
    return outbox.pending
