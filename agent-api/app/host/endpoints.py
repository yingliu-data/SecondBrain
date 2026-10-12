"""endpoints — the host side of the vsock ports, one set per session.

The host listens on Unix sockets `{session_dir}/ipc/v.sock_{port}`
(protocol.ports.socket_name; bound with app.host.uds.bind_unix, so a session
directory too deep for sun_path still works). That is
exactly where Firecracker delivers guest-initiated vsock connections when the
VM's vsock `uds_path` is `{session_dir}/ipc/v.sock`, and exactly what the
test double (the guest as a subprocess with `--connect unix:{session_dir}/ipc`)
connects to. One listener code path serves both.

  Endpoints(session_dir, ports)    create/unlink/chmod the listeners
    .accept_control(timeout)       blocking accept on :5000 (call from a thread);
                                   while :5000 is closed it waits for reopen()
                                   (Condition + a wake socket, no polling)
    .listener(port)                the raw listening socket (the LLM proxy takes :5010)
    .close_port(port) .reopen(port)  no listener while the guest is parked
    .close()                       close and unlink everything

  ControlChannel(sock, loop, inbound)
    one connected :5000 socket: a reader thread parses JSONL lines with
    protocol.messages.parse and hands ("event"|"control"|"ack"|"live", msg)
    to the asyncio loop via loop.call_soon_threadsafe(inbound.put_nowait, ...);
    a writer thread drains send() so the loop never blocks on the socket.
    Link loss posts ("link", "down", channel).
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import select
import socket
import threading
import time
from pathlib import Path

from protocol.messages import ProtocolError, encode, parse
from protocol.ports import CONTROL, SOCKET_PREFIX, socket_name

from app.host.uds import bind_unix

logger = logging.getLogger("host.endpoints")


def socket_path(session_dir: Path, port: int) -> Path:
    return Path(session_dir) / "ipc" / socket_name(port)


class Endpoints:
    """Listeners for one session. accept()/accept_control() wait in poll()
    on the listener and a wake socket: close_port(), reopen() and close()
    wake a waiting accept at once (no polling sleep while a port is closed;
    a reopen right before a restore is seen immediately)."""

    def __init__(self, session_dir: Path, ports) -> None:
        self.session_dir = Path(session_dir)
        self.ipc = self.session_dir / "ipc"
        self.ipc.mkdir(parents=True, exist_ok=True)
        if isinstance(ports, dict):
            ports = ports.values()
        self.ports = sorted(set(int(p) for p in ports))
        self._cv = threading.Condition()           # guards _listeners / _closed
        self._closed = False
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._listeners: dict[int, socket.socket] = {}
        for port in self.ports:
            self._listeners[port] = self._listen(port)

    def _listen(self, port: int) -> socket.socket:
        path = socket_path(self.session_dir, port)
        try:
            os.unlink(path)  # stale socket from an earlier run
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bind_unix(sock, path)
        os.chmod(path, 0o600)
        sock.listen(8)
        return sock

    def _wake(self) -> None:
        """Interrupt a poll() in accept(); the caller holds no lock."""
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass  # buffer full (a wake is already pending) or closed

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except OSError:
            pass

    @property
    def uds_path(self) -> Path:
        """The Firecracker vsock `uds_path`; port P lands on `{uds_path}_P`."""
        return self.ipc / SOCKET_PREFIX

    def path(self, port: int) -> Path:
        return socket_path(self.session_dir, port)

    def listener(self, port: int) -> socket.socket:
        return self._listeners[port]

    def accept(self, port: int, timeout: float | None) -> socket.socket | None:
        """Blocking accept on `port`; None on timeout or after close(). While
        the port is closed (close_port) it waits for reopen() without polling;
        a close_port() during the wait ends the poll and the wait goes on
        for the reopened listener."""
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        while True:
            with self._cv:
                if self._closed:
                    return None
                lst = self._listeners.get(port)
                if lst is None:
                    left = remaining()
                    if left == 0.0:
                        return None
                    self._cv.wait(left)            # close_port .. reopen / close
                    continue
                wake = self._wake_r
            left = remaining()
            try:
                # poll, not select: a busy host has fds above FD_SETSIZE
                poller = select.poll()
                lfd = lst.fileno()
                poller.register(lfd, select.POLLIN)
                poller.register(wake, select.POLLIN)
                ready = {fd for fd, _ in poller.poll(None if left is None else left * 1000)}
            except (OSError, ValueError):          # closed under us: look again
                if left == 0.0:
                    return None
                continue
            if wake.fileno() in ready:
                self._drain_wake()
            if lfd in ready:
                try:
                    lst.settimeout(0)
                    conn, _ = lst.accept()
                except (BlockingIOError, InterruptedError, socket.timeout):
                    conn = None                    # taken or reset since poll
                except OSError:
                    with self._cv:
                        if self._closed or self._listeners.get(port) is not lst:
                            continue               # closed under us: look again
                    return None
                if conn is not None:
                    conn.settimeout(None)
                    return conn
            if left == 0.0:
                return None

    def accept_control(self, timeout: float | None = None) -> socket.socket | None:
        """accept(CONTROL, timeout): while :5000 is closed (the guest parked)
        it sleeps until reopen(CONTROL), close() or the timeout."""
        return self.accept(CONTROL, timeout)

    def close_port(self, port: int) -> None:
        """Stop listening on one port (between the acked `parked` and the
        snapshot, so the guest's idle-wait loop finds no listener and stays
        idle; a pending, not yet accepted connection is dropped with it)."""
        with self._cv:
            lst = self._listeners.pop(port, None)
            if lst is not None:
                try:
                    lst.close()
                except OSError:
                    pass
            try:
                os.unlink(socket_path(self.session_dir, port))
            except FileNotFoundError:
                pass
            self._cv.notify_all()
        self._wake()

    def reopen(self, port: int) -> socket.socket:
        """Listen on `port` again (before a restore); no-op when open. Wakes
        an accept() waiting for the port at once."""
        with self._cv:
            lst = self._listeners.get(port)
            if lst is None:
                lst = self._listeners[port] = self._listen(port)
                self._cv.notify_all()
        return lst

    def close(self) -> None:
        with self._cv:
            self._closed = True
            ports = list(self._listeners)
        for port in ports:
            self.close_port(port)
        with self._cv:
            self._cv.notify_all()
        self._wake()
        # a waiter that already took these sockets gets EBADF/ValueError
        # from poll and finds _closed on its next look
        for s in (self._wake_w, self._wake_r):
            try:
                s.close()
            except OSError:
                pass


class ControlChannel:
    """One connected control socket. Thread-safe send; inbound to the loop."""

    def __init__(self, sock: socket.socket, loop: asyncio.AbstractEventLoop,
                 inbound: asyncio.Queue, *, name: str = "") -> None:
        self.sock = sock
        self.loop = loop
        self.inbound = inbound
        self.name = name
        self._out: queue.Queue = queue.Queue()
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"ctl-reader{name}")
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name=f"ctl-writer{name}")
        self._reader.start()
        self._writer.start()

    # -- loop side
    def send(self, msg: dict) -> None:
        self._out.put(encode(msg))

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._out.put(None)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- threads
    def _post(self, item) -> None:
        try:
            self.loop.call_soon_threadsafe(self.inbound.put_nowait, item)
        except RuntimeError:
            pass  # loop closed

    def _read_loop(self) -> None:
        try:
            with self.sock.makefile("rb") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        kind, msg = parse(line)
                    except ProtocolError as e:
                        logger.warning("control line rejected: %s (%r)", e, line[:200])
                        continue
                    self._post((kind, msg))
        except (OSError, ValueError):
            pass
        finally:
            self._post(("link", "down", self))
            self.close()

    def _write_loop(self) -> None:
        while True:
            item = self._out.get()
            if item is None:
                return
            try:
                self.sock.sendall(item.encode("utf-8"))
            except OSError as e:
                logger.info("control send failed: %s", e)
                self.close()
                return
