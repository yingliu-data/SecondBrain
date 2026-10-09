"""transport — the :5000 control channel from the guest's side
(docs/PROTOCOL.md, docs/GUEST.md).

The guest never listens. A `Connector` opens an outbound stream to a port:
`VsockConnector(cid=2)` over AF_VSOCK in the microVM, `UnixConnector(dir)`
over the Unix socket `{dir}/v.sock_{port}` in tests (the name Firecracker
gives the host side of a guest-initiated vsock connection).

`Transport(connector, inbound, session_id=None)` runs two threads:

  link thread    connects (backoff 0.1 s doubling to 5 s), sends the handshake
                 when the session is known, then reads JSONL lines until the
                 stream drops; every line goes through protocol.messages.parse.
                 Controls pass protocol.messages.Inbox (dedupe / gap) and are
                 posted as ("control", msg) once each; every control is acked.
                 Host acks of events are applied to the Outbox and posted as
                 ("ack", n). Connection changes are posted as ("link", "up"|"down").
  writer thread  the single socket writer, fed by an internal queue. Events
                 are kept in the Outbox until acked; live messages and acks are
                 fire-and-forget and dropped while the link is down.

Reconnect (see docs/PROTOCOL.md): on a new connection with a known session the
first line is the handshake (ack of the highest contiguous cseq delivered);
the host answers with its ack of seq; `resend_plan` applies it and yields what
to resend; the resend goes out before anything newer (a connection generation
tags queued items, the writer drops items from an older generation because
the Outbox already covers them); then ("link", "up") is posted.

The session_id is learned from the first control message (hello) when the
transport is built without one. A fresh transport takes the hello's own cseq
as its floor (a resumed session's hello is not cseq 1); controls that
arrive before the hello with a higher cseq are gaps and are never delivered.

Idle-wait (the snapshot point): built with `idle=True`, the link thread
tries to connect every `idle_poll_s` (IDLE_POLL_S, 50 ms), silently and
without backoff, and stays in that loop until the host has sent a first line
on a connection (any connect error, or a reset before that, e.g. the vsock
reset a Firecracker snapshot causes, is retried the same way);
`wait_awake(timeout)` blocks until that first line came, `wait_up(timeout)`
until a connection is usable. `park()` closes the connection and puts
the link thread back into that idle loop (the guest keeps its state, the
worker snapshots the VM, and after a restore the first attempt reconnects
with the normal handshake + resend). `seed_seq(n)` continues the event
stream after a log return: the host holds 1..n, the next event is n + 1.
Bulk (:5001) is guest/bulk.py: one connection per transfer, not this class.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading
from typing import Protocol

from protocol import messages
from protocol.messages import ProtocolError

log = logging.getLogger("guest.transport")

CONTROL_PORT = 5000
BACKOFF_INITIAL_S = 0.1
BACKOFF_MAX_S = 5.0
IDLE_POLL_S = 0.05


# ---------------------------------------------------------------- connectors


class Connector(Protocol):
    def connect(self, port: int) -> socket.socket: ...


def socket_path(dir, port: int) -> str:
    """The Unix-socket double of vsock port `port`: {dir}/v.sock_{port}."""
    return os.path.join(str(dir), f"v.sock_{port}")


class UnixConnector:
    """Test transport: Unix sockets named after the vsock ports."""

    def __init__(self, dir):
        self.dir = str(dir)

    def path(self, port: int) -> str:
        return socket_path(self.dir, port)

    def connect(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.path(port))
        except OSError:
            sock.close()
            raise
        return sock

    def __repr__(self) -> str:
        return f"UnixConnector({self.dir!r})"


class VsockConnector:
    """The real thing: AF_VSOCK to the host (CID 2)."""

    def __init__(self, cid: int = 2):
        self.cid = cid

    def connect(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        try:
            sock.connect((self.cid, port))
        except OSError:
            sock.close()
            raise
        return sock

    def __repr__(self) -> str:
        return f"VsockConnector(cid={self.cid})"


def _shutdown(sock: socket.socket) -> None:
    """Wake any thread blocked on this socket, then close it."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------- transport


class Transport:
    def __init__(self, connector, inbound: queue.Queue, session_id: str | None = None,
                 port: int = CONTROL_PORT, backoff_initial: float = BACKOFF_INITIAL_S,
                 backoff_max: float = BACKOFF_MAX_S, idle: bool = False,
                 idle_poll_s: float = IDLE_POLL_S):
        self.connector = connector
        self.inbound = inbound
        self.port = port
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.idle_poll_s = idle_poll_s
        self._idle = idle                              # poll silently, no backoff

        self.session_id: str | None = None
        self.inbox: messages.Inbox | None = None    # host -> guest controls (cseq)
        self.outbox: messages.Outbox | None = None  # guest -> host events (seq)
        if session_id is not None:
            self._set_session(session_id)

        self._lock = threading.RLock()                 # session, outbox, generation
        self._ack_cv = threading.Condition(self._lock)
        self._wq: queue.Queue = queue.Queue()          # writer thread's feed
        self._gen = 0                                  # bumped on every link change
        self._up = False
        self._sock: socket.socket | None = None        # the link thread's connection
        self._stop = threading.Event()
        self._link_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self.connections = 0                           # successful connects so far
        self._up_event = threading.Event()
        self._awake = threading.Event()                # set while not idle-waiting
        if not idle:
            self._awake.set()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._link_thread is not None:
            raise RuntimeError("transport already started")
        self._writer_thread = threading.Thread(
            target=self._writer, name="guest-transport-writer", daemon=True)
        self._link_thread = threading.Thread(
            target=self._link, name="guest-transport-link", daemon=True)
        self._writer_thread.start()
        self._link_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            _shutdown(sock)
        self._wq.put(None)
        for t in (self._link_thread, self._writer_thread):
            if t is not None and t is not threading.current_thread():
                t.join(timeout)

    @property
    def connected(self) -> bool:
        return self._up

    @property
    def idle(self) -> bool:
        """True while the link thread is in the idle-wait loop."""
        return self._idle

    def wait_awake(self, timeout: float | None = None) -> bool:
        """Block until idle-wait is over (the host sent its first line).
        False on timeout."""
        return self._awake.wait(timeout)

    def wait_up(self, timeout: float | None = None) -> bool:
        """Block until a connection is usable (handshake done). False on timeout."""
        return self._up_event.wait(timeout)

    def park(self) -> None:
        """Close the connection and return to the idle-wait loop (park):
        state, Outbox and Inbox are kept; the next connection that succeeds
        opens with the handshake and resends whatever is unacked."""
        with self._lock:
            self._idle = True
            self._awake.clear()
        sock = self._sock
        if sock is not None:
            _shutdown(sock)

    def seed_seq(self, last_seq: int) -> None:
        """The host already holds events 1..last_seq (a log return on resume):
        the next event sent is last_seq + 1 and nothing below it is resent."""
        with self._ack_cv:
            if self.outbox is None:
                raise RuntimeError("session_id unknown: nothing to seed")
            self.outbox.resume_from(last_seq)
            self._ack_cv.notify_all()

    @property
    def last_delivered_cseq(self) -> int:
        return self.inbox.last_delivered if self.inbox is not None else 0

    # ------------------------------------------------------------ sending

    def send_event(self, event: dict) -> None:
        """Queue a sequenced event. It is held in the Outbox until the host
        acks it and resent after a reconnect. Raises ProtocolError when the
        event is out of order or belongs to another session."""
        with self._lock:
            if self.outbox is None:
                self._set_session(event.get("session_id"))
            self.outbox.push(event)
            gen = self._gen
        self._wq.put(("event", gen, event))

    def send_live(self, type: str, body: dict) -> None:
        """Fire-and-forget: dropped when the link is down."""
        msg = messages.live(type, body)
        self._wq.put(("live", self._gen, msg))

    def send_ack(self, cseq: int) -> None:
        if self.session_id is None:
            raise RuntimeError("session_id unknown: nothing to ack yet")
        self._wq.put(("live", self._gen, messages.ack(self.session_id, ack_cseq=cseq)))

    def wait_acked(self, seq: int, timeout: float | None = None) -> bool:
        """Block until the host has acked `seq` (or beyond). False on timeout."""
        with self._ack_cv:
            return self._ack_cv.wait_for(
                lambda: self.outbox is not None and self.outbox.highest_acked >= seq,
                timeout)

    @property
    def acked_seq(self) -> int:
        return self.outbox.highest_acked if self.outbox is not None else 0

    # ------------------------------------------------------------ session

    def _set_session(self, session_id) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ProtocolError(f"session_id must be a non-empty string, got {session_id!r}")
        self.session_id = session_id
        self.inbox = messages.Inbox(session_id, "cseq")
        self.outbox = messages.Outbox(session_id, "seq")

    # ------------------------------------------------------------ link thread

    def _link(self) -> None:
        delay = self.backoff_initial
        while not self._stop.is_set():
            idle = self._idle
            try:
                sock = self.connector.connect(self.port)
            except OSError as exc:
                if idle:        # the snapshot point: poll silently, never back off
                    self._stop.wait(self.idle_poll_s)
                    continue
                log.debug("connect to :%d failed (%s); retry in %.1fs", self.port, exc, delay)
                self._stop.wait(delay)
                delay = min(delay * 2, self.backoff_max)
                continue
            # idle-wait ends with the first line the host sends, not with the
            # connect: a connection reset right after it (a snapshot restore
            # resets vsock connections) goes back to silent polling
            delay = self.backoff_initial
            self.connections += 1
            self._sock = sock
            try:
                self._serve(sock)
            except OSError as exc:
                log.info("control link lost: %s", exc)
            finally:
                self._sock = None
                was_up = self._link_down()
                _shutdown(sock)
            if was_up and not self._stop.is_set():
                self.inbound.put(("link", "down"))
            if self._idle:
                self._stop.wait(self.idle_poll_s)

    def _serve(self, sock: socket.socket) -> None:
        """One connection: handshake, then read lines until the stream ends."""
        handshaking = self.session_id is not None
        if handshaking:
            line = messages.encode(messages.handshake(self.session_id, self.inbox.last_delivered))
            sock.sendall(line.encode("utf-8"))
        else:
            self._link_up(sock, peer_ack=None)

        reader = sock.makefile("rb")
        for raw in reader:
            if self._stop.is_set():
                return
            if self._idle:
                with self._lock:
                    self._idle = False
                    self._awake.set()
                log.info("idle-wait over: the host answered on :%d", self.port)
            if not raw.strip():
                continue
            try:
                kind, msg = messages.parse(raw)
            except ProtocolError as exc:
                log.warning("dropping unparseable line from host: %s", exc)
                continue
            if kind == "ack":
                n = msg.get("ack_seq")
                if n is None:
                    log.warning("host sent an ack_cseq; ignored")
                    continue
                if self.session_id is not None and msg["session_id"] != self.session_id:
                    log.error("ack for foreign session %r ignored", msg["session_id"])
                    continue
                if handshaking:
                    handshaking = False
                    self._link_up(sock, peer_ack=n)
                else:
                    self._apply_ack(n)
            elif kind == "control":
                self._on_control(msg)
            else:
                log.warning("unexpected %s line from host ignored", kind)

    def _on_control(self, msg: dict) -> None:
        with self._lock:
            if self.session_id is None:
                self._set_session(msg["session_id"])
            if (msg["type"] == "hello" and self.inbox.last_delivered == 0
                    and msg["session_id"] == self.session_id and msg["cseq"] > 1):
                # a fresh process taking over a session: the hello's cseq is the floor
                self.inbox.resume_from(msg["cseq"] - 1)
            try:
                verdict = self.inbox.accept(msg)
            except ProtocolError as exc:
                log.error("control message dropped: %s", exc)
                return
            ack = self.inbox.ack()
            gen = self._gen
        if verdict == "new":
            self.inbound.put(("control", msg))
        elif verdict == "gap":
            log.warning("control cseq %s ahead of %s: waiting for the resend",
                        msg["cseq"], ack["ack_cseq"])
        # new: ack it; duplicate: tell the host again; gap: re-ack the prefix
        self._wq.put(("live", gen, ack))

    def _apply_ack(self, n: int) -> None:
        with self._ack_cv:
            if self.outbox is None:
                if n != 0:
                    log.error("ack_seq %d before any event was sent", n)
                return
            before = self.outbox.highest_acked
            try:
                self.outbox.acknowledge(n)
            except ProtocolError as exc:
                log.error("host ack ignored: %s", exc)
                return
            if self.outbox.highest_acked == before:
                return      # stale: nothing new for the coordinator
            self._ack_cv.notify_all()
        self.inbound.put(("ack", n))

    def _link_up(self, sock: socket.socket, peer_ack: int | None) -> None:
        """The connection is usable: hand it to the writer together with the
        resend list and a fresh ack of the controls delivered so far."""
        with self._ack_cv:
            self._gen += 1
            gen = self._gen
            resend: list[dict] = []
            advanced = False
            if peer_ack is not None and self.outbox is not None:
                before = self.outbox.highest_acked
                try:
                    resend = messages.resend_plan(self.outbox, peer_ack)
                except ProtocolError as exc:
                    log.error("handshake ack %d rejected (%s); resending everything held",
                              peer_ack, exc)
                    resend = self.outbox.pending
                advanced = self.outbox.highest_acked > before
                self._ack_cv.notify_all()
            ack = self.inbox.ack() if self.inbox is not None else None
            self._up = True
            self._up_event.set()
            self._wq.put(("up", gen, sock, resend, ack))
        if advanced:
            self.inbound.put(("ack", self.outbox.highest_acked))
        self.inbound.put(("link", "up"))

    def _link_down(self) -> bool:
        with self._lock:
            was_up = self._up
            self._up = False
            self._up_event.clear()
            self._gen += 1      # items queued while down are void: the Outbox covers them
        return was_up

    # ------------------------------------------------------------ writer thread

    def _writer(self) -> None:
        sock: socket.socket | None = None
        gen = 0
        while True:
            item = self._wq.get()
            if item is None:
                return
            if item[0] == "up":
                _, gen, sock, resend, ack = item
                lines = [messages.encode(e) for e in resend]
                if ack is not None:
                    lines.append(messages.encode(ack))
                if lines and not self._write(sock, lines):
                    sock = None
                continue
            if sock is None or item[1] != gen:
                continue    # link down, or queued for an earlier connection
            if not self._write(sock, [messages.encode(item[2])]):
                sock = None

    @staticmethod
    def _write(sock: socket.socket, lines: list[str]) -> bool:
        try:
            sock.sendall("".join(lines).encode("utf-8"))
            return True
        except OSError as exc:
            log.debug("write on control link failed: %s", exc)
            _shutdown(sock)     # wakes the reader; the link thread reconnects
            return False
