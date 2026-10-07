"""endpoints — the host side of the vsock ports, one set per session.

The host listens on Unix sockets `{session_dir}/ipc/v.sock_{port}`. That is
exactly where Firecracker delivers guest-initiated vsock connections when the
VM's vsock `uds_path` is `{session_dir}/ipc/v.sock`, and exactly what the
test double (the guest as a subprocess with `--connect unix:{session_dir}/ipc`)
connects to. One listener code path serves both.

  Endpoints(session_dir, ports)    create/unlink/chmod the listeners
    .accept_control(timeout)       blocking accept on :5000 (call from a thread)
    .listener(port)                the raw listening socket (the LLM proxy takes :5010)
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
import socket
import threading
from pathlib import Path

from protocol.messages import ProtocolError, encode, parse

logger = logging.getLogger("host.endpoints")

SOCKET_PREFIX = "v.sock"


def socket_path(session_dir: Path, port: int) -> Path:
    return Path(session_dir) / "ipc" / f"{SOCKET_PREFIX}_{port}"


class Endpoints:
    def __init__(self, session_dir: Path, ports) -> None:
        self.session_dir = Path(session_dir)
        self.ipc = self.session_dir / "ipc"
        self.ipc.mkdir(parents=True, exist_ok=True)
        if isinstance(ports, dict):
            ports = ports.values()
        self.ports = sorted(set(int(p) for p in ports))
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
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen(8)
        return sock

    @property
    def uds_path(self) -> Path:
        """The Firecracker vsock `uds_path`; port P lands on `{uds_path}_P`."""
        return self.ipc / SOCKET_PREFIX

    def path(self, port: int) -> Path:
        return socket_path(self.session_dir, port)

    def listener(self, port: int) -> socket.socket:
        return self._listeners[port]

    def accept(self, port: int, timeout: float | None) -> socket.socket | None:
        """Blocking accept on `port`; None on timeout or after close()."""
        lst = self._listeners.get(port)
        if lst is None:
            return None
        lst.settimeout(timeout)
        try:
            conn, _ = lst.accept()
        except socket.timeout:
            return None
        except OSError:
            return None
        conn.settimeout(None)
        return conn

    def accept_control(self, timeout: float | None = None) -> socket.socket | None:
        return self.accept(5000, timeout)

    def close(self) -> None:
        for port, lst in list(self._listeners.items()):
            try:
                lst.close()
            except OSError:
                pass
            try:
                os.unlink(socket_path(self.session_dir, port))
            except FileNotFoundError:
                pass
        self._listeners.clear()


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
