"""fake_host — the host double every guest test talks to.

Listens on `{dir}/v.sock_5000` (the Unix-socket double of vsock port 5000:
the name Firecracker gives the host side of a guest-initiated connection) and
speaks docs/PROTOCOL.md from the host's side with protocol.messages:

  - on the first connection it sends `hello` (cseq 1) unless auto_hello=False
  - every event line goes through its Inbox: new ones land in `events` (in
    arrival order, no duplicates, no gaps) and are acked (unless acks are
    paused); duplicates are re-acked; every arrival is kept in `raw_events`
  - a `tool_call` event whose side is not guest is answered with a
    `side_result` control from `side_results` (dict call_id -> {content, ok},
    or a callable(tool_call body) -> {content, ok})
  - it survives reconnects: on a new connection it expects the guest's
    handshake (an ack of cseq), answers with its own ack of seq and resends
    the unacked controls above the guest's ack (resend_plan)

Test controls: send(type, body) sends the next control; drop_link() closes
the current connection; pause_acks()/resume_acks(); redeliver(msg) writes a
control line again (a duplicate on the wire); wait_for(type, timeout) and the
other wait_* helpers block on the state the guest produces.
"""
from __future__ import annotations

import os
import socket
import threading

from protocol import messages
from protocol.messages import Inbox, Outbox, ProtocolError, encode

CONTROL_PORT = 5000


class FakeHost:
    def __init__(self, dir, hello_body: dict, side_results=None, auto_ack: bool = True,
                 port: int = CONTROL_PORT, auto_hello: bool = True):
        self.dir = str(dir)
        self.port = port
        self.path = os.path.join(self.dir, f"v.sock_{port}")
        self.hello_body = hello_body
        self.session_id = hello_body["session_id"]
        self.side_results = side_results if side_results is not None else {}
        self.auto_ack = auto_ack
        self.auto_hello = auto_hello

        self.outbox = Outbox(self.session_id, "cseq")   # controls we sent
        self.inbox = Inbox(self.session_id, "seq")      # events we delivered
        self.events: list[dict] = []        # delivered events, arrival order
        self.raw_events: list[dict] = []    # every event line, duplicates included
        self.live: list[dict] = []          # live messages, arrival order
        self.acks: list[int] = []           # every ack_cseq the guest sent
        self.handshakes: list[int] = []     # the ack_cseq that opened each reconnect
        self.acked_seq = 0                  # highest ack_seq we sent
        self.connections = 0
        self.errors: list[str] = []         # protocol trouble seen on the wire

        self._cv = threading.Condition()
        self._wlock = threading.Lock()
        self._conn: socket.socket | None = None
        self._paused = False
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "FakeHost":
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(4)
        self._thread = threading.Thread(target=self._accept_loop, name="fake-host", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.drop_link()
        if self._listener is not None:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._listener.close()
        if self._thread is not None:
            self._thread.join(2.0)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------ test controls

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def guest_acked_cseq(self) -> int:
        return self.outbox.highest_acked

    def send(self, type: str, body: dict, deliver: bool = True) -> dict:
        """Send the next control message. With deliver=False it is only
        recorded as pending (a later handshake resends it)."""
        with self._cv:
            msg = messages.control_message(self.session_id, self.outbox.next_number(), type, body)
            self.outbox.push(msg)
        if deliver:
            self._write(msg)
        return msg

    def redeliver(self, msg: dict) -> None:
        """Write a control line again: a duplicate on the wire."""
        self._write(msg)

    def write_raw(self, text: str) -> None:
        """Put arbitrary bytes on the wire (garbage lines for the parser)."""
        with self._wlock:
            conn = self._conn
            if conn is not None:
                try:
                    conn.sendall(text.encode("utf-8"))
                except OSError:
                    pass

    def drop_link(self) -> None:
        with self._wlock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def pause_acks(self) -> None:
        with self._cv:
            self._paused = True

    def resume_acks(self) -> None:
        with self._cv:
            self._paused = False
        self._send_ack()

    # ------------------------------------------------------------ waiting

    def wait_for(self, type: str, timeout: float = 5.0, after_seq: int = 0) -> dict:
        """The first delivered event of `type` with seq > after_seq."""
        def found():
            for ev in self.events:
                if ev["type"] == type and ev["seq"] > after_seq:
                    return ev
            return None
        with self._cv:
            if not self._cv.wait_for(found, timeout):
                raise TimeoutError(f"no {type!r} event after seq {after_seq} within {timeout}s")
            return found()

    def wait_for_seq(self, seq: int, timeout: float = 5.0) -> None:
        with self._cv:
            if not self._cv.wait_for(lambda: self.inbox.last_delivered >= seq, timeout):
                raise TimeoutError(f"seq {seq} not delivered within {timeout}s "
                                   f"(have {self.inbox.last_delivered})")

    def wait_connected(self, timeout: float = 5.0, count: int | None = None) -> None:
        """Until a connection is open (and `connections` >= count when given)."""
        target = count if count is not None else 1
        with self._cv:
            if not self._cv.wait_for(
                    lambda: self._conn is not None and self.connections >= target, timeout):
                raise TimeoutError(f"no connection #{target} within {timeout}s")

    def wait_disconnected(self, timeout: float = 5.0) -> None:
        with self._cv:
            if not self._cv.wait_for(lambda: self._conn is None, timeout):
                raise TimeoutError(f"still connected after {timeout}s")

    def wait_acked_cseq(self, cseq: int, timeout: float = 5.0) -> None:
        with self._cv:
            if not self._cv.wait_for(lambda: self.outbox.highest_acked >= cseq, timeout):
                raise TimeoutError(f"guest did not ack cseq {cseq} within {timeout}s")

    def wait_live(self, type: str, timeout: float = 5.0, index: int = 0) -> dict:
        """The index-th live message of `type`."""
        def found():
            hits = [m for m in self.live if m["type"] == type]
            return hits[index] if len(hits) > index else None
        with self._cv:
            if not self._cv.wait_for(found, timeout):
                raise TimeoutError(f"no live {type!r} #{index} within {timeout}s")
            return found()

    # ------------------------------------------------------------ wire

    def _write(self, msg: dict) -> bool:
        with self._wlock:
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.sendall(encode(msg).encode("utf-8"))
                return True
            except OSError:
                return False

    def _send_ack(self) -> None:
        with self._cv:
            if self._paused:
                return
            n = self.inbox.last_delivered
            self.acked_seq = max(self.acked_seq, n)
        self._write(messages.ack(self.session_id, ack_seq=n))

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._cv:
                self.connections += 1
                first = self.connections == 1
                self._conn = conn
                self._cv.notify_all()
            try:
                self._serve(conn, first)
            except OSError:
                pass
            finally:
                with self._wlock:
                    if self._conn is conn:
                        self._conn = None
                try:
                    conn.close()
                except OSError:
                    pass
                with self._cv:
                    self._cv.notify_all()

    def _serve(self, conn: socket.socket, first: bool) -> None:
        if first and self.auto_hello:
            self.send("hello", self.hello_body)
        first_line = True
        for raw in conn.makefile("rb"):
            if self._stop.is_set():
                return
            if not raw.strip():
                continue
            try:
                kind, msg = messages.parse(raw)
            except ProtocolError as exc:
                self._error(f"unparseable line: {exc}")
                continue
            # the guest's first line on a reconnect is its handshake (an ack of
            # cseq); on the first connection with auto_hello it is just the
            # ack of the hello we sent
            handshake = (first_line and kind == "ack" and "ack_cseq" in msg
                         and not (first and self.auto_hello))
            first_line = False
            if kind == "ack":
                self._on_ack(msg, handshake)
            elif kind == "event":
                self._on_event(msg)
            elif kind == "live":
                with self._cv:
                    self.live.append(msg)
                    self._cv.notify_all()
            else:
                self._error(f"unexpected {kind} line from guest")

    def _on_ack(self, msg: dict, handshake: bool) -> None:
        if "ack_cseq" not in msg:
            self._error("guest sent an ack_seq")
            return
        n = msg["ack_cseq"]
        with self._cv:
            self.acks.append(n)
            try:
                if handshake:
                    self.handshakes.append(n)
                    resend = messages.resend_plan(self.outbox, n)
                else:
                    self.outbox.acknowledge(n)
                    resend = []
            except ProtocolError as exc:
                self._error(f"guest ack {n}: {exc}")
                return
            finally:
                self._cv.notify_all()
        if handshake:
            self._send_ack()
            for m in resend:
                self._write(m)

    def _on_event(self, ev: dict) -> None:
        with self._cv:
            self.raw_events.append(ev)
            try:
                verdict = self.inbox.accept(ev)
            except ProtocolError as exc:
                self._error(f"event seq {ev.get('seq')}: {exc}")
                return
            if verdict == "new":
                self.events.append(ev)
            self._cv.notify_all()
        if verdict == "new" and ev["type"] == "tool_call" and ev["body"].get("side") != "guest":
            self._answer_tool_call(ev["body"])
        if self.auto_ack and verdict != "gap":
            self._send_ack()

    def _answer_tool_call(self, call: dict) -> None:
        call_id = call["call_id"]
        if callable(self.side_results):
            result = self.side_results(call)
        else:
            result = self.side_results.get(call_id)
        if result is None:
            result = {"content": f"fake host: no result scripted for {call_id}", "ok": False}
        self.send("side_result", {
            "call_id": call_id, "side": call["side"],
            "content": result.get("content", ""), "ok": bool(result.get("ok", False)),
        })

    def _error(self, text: str) -> None:
        with self._cv:
            self.errors.append(text)
            self._cv.notify_all()
