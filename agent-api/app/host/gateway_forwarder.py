"""gateway_forwarder — :5011 → mcp-gateway.

A threaded HTTP/1.1 relay on the session's :5011 Unix socket (uds_http.py,
the same server as the LLM proxy). It proxies **only `POST /mcp`** to
`{MCP_GATEWAY_URL}/mcp`; any other method or path is a 404 and never leaves
the host (the forwarder adds the host's bearer, so a guest must not reach
`/internal/*` through it).

Headers. The guest's request headers are passed on except hop-by-hop ones,
`Host`, `Content-Length`, the guest's own `Authorization` and every `X-SB-*`
header but `X-SB-Call`; the forwarder then sets `X-SB-User`, `X-SB-Tenant`,
`X-SB-Session` (the event-log session_id) and `Authorization: Bearer
$INTERNAL_TOKEN` (when the token is set). `X-SB-Reattach` is never forwarded.

Answer. The gateway's status and body go back verbatim (with its
Content-Type and Mcp-* headers). The read timeout is
MCP_GATEWAY_CALL_TIMEOUT_S, above the longest upstream timeout_s (a call can
run 45 minutes). A gateway that cannot be reached is a 502 JSON body (uds_http.error_body) and a
`gateway{op: error, source: forwarder}` host event (the gateway cannot log
its own outage).

One relay path. Every request runs in its own thread, independent of the
guest's connection, and its answer (buffered, JSON or text/event-stream
alike) is kept in the worker-lived GatewayRelay table `{key: running |
done(status, body)}`. The key is the request's `X-SB-Call` (long calls
survive a park); a request without one (tools/list, ...) gets an internal
key that no guest can name, so it is never re-attachable and its entry goes
when the guest leaves. The handler blocks on the guest's connection and the
entry's wake pipe together (no polling). For an `X-SB-Call` entry:
  - the guest's connection drops (park, snapshot, link loss): the gateway
    request keeps running, its answer is stored
  - the same call_id again (with or without `X-SB-Reattach: 1`): joins the
    running call, or gets the stored answer at once — the gateway sees one
    request
  - an entry is dropped once its answer was written to a guest; a later
    first send (no X-SB-Reattach) with that call_id is a new request (the
    `sb/input_response` re-send)
  - an unknown call_id with `X-SB-Reattach: 1` → 410, never re-executed
  - the worker discards the entry of a call the guest declared `lost`
The relay outlives the forwarder: closing :5011 around a park stops
accepting, never a held call.

  GatewayRelay(gateway_url, user=, tenant=, session=, host_event=None,
               token=INTERNAL_TOKEN, timeout_s=MCP_GATEWAY_CALL_TIMEOUT_S)
    .held(key, reattach, body, headers) .discard(call_id) .close()
    (gateway_url must be set: no relay is made without a gateway)
  GatewayForwarder(listener, relay)   .start() / .stop(join=True)
"""
from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time
import weakref
from typing import Callable

import httpx

from protocol.clock import elapsed_ms
from protocol.mcp import CALL_HEADER, REATTACH_HEADER

from app.config import INTERNAL_TOKEN, MCP_GATEWAY_CALL_TIMEOUT_S
from app.gateway.client import auth_headers, identity_headers
from app.host.uds_http import UDSHandler, UDSService, error_body

logger = logging.getLogger("host.gateway_forwarder")

MCP_PATH = "/mcp"
_CALL = CALL_HEADER.lower()
# never forwarded from the guest (hop-by-hop, recomputed, or the host's to set)
_DROP = {"host", "content-length", "connection", "keep-alive", "proxy-connection",
         "proxy-authorization", "proxy-authenticate", "te", "trailer", "trailers",
         "transfer-encoding", "upgrade", "authorization"}
# gateway response headers passed back besides Content-Type
_PASS_BACK = ("mcp-session-id", "mcp-protocol-version")


def guest_headers(headers) -> dict:
    """The guest's headers minus hop-by-hop, Authorization and any X-SB-*
    except X-SB-Call."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _DROP or (lk.startswith("x-sb-") and lk != _CALL):
            continue
        out[k] = v
    return out


def _close_fds(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


class HeldCall:
    """One gateway request: running until `done` is set, then the answer.
    finish() also writes to a wake pipe that is never drained, so every
    waiter's select() sees it (level-triggered)."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.status = 0
        self.body = b""
        self.content_type = "application/json"
        self.headers: dict[str, str] = {}
        self._wake_r, self._wake_w = os.pipe()
        weakref.finalize(self, _close_fds, self._wake_r, self._wake_w)

    def finish(self, status: int, body: bytes, content_type: str = "application/json",
               headers: dict | None = None) -> None:
        self.status, self.body, self.content_type = status, body, content_type
        self.headers = headers or {}
        self.done.set()
        os.write(self._wake_w, b"x")

    def wait(self, conn: socket.socket) -> bool:
        """Block until the answer is in (True) or the guest closed `conn`
        (False: the call runs on, a re-attach gets it)."""
        # poll, not select: a busy worker's fds go past FD_SETSIZE (1024)
        try:
            conn_fd = conn.fileno()
        except (OSError, ValueError):
            return False
        poller = select.poll()
        poller.register(conn_fd, select.POLLIN)
        poller.register(self._wake_r, select.POLLIN)
        while not self.done.is_set():
            try:
                ready = poller.poll()
            except OSError:
                return False
            if any(fd == conn_fd for fd, _ in ready):
                if _peer_gone(conn):
                    return False
                poller.unregister(conn_fd)   # pipelined bytes: only the answer now
        return True


class GatewayRelay:
    """The worker-lived half: the gateway client, the identity stamp and the
    held-call table. Survives the :5011 listener closing around a park."""

    def __init__(self, gateway_url: str, *, user: str, tenant: str, session: str,
                 host_event: Callable[[str, dict], object] | None = None,
                 token: str = INTERNAL_TOKEN,
                 timeout_s: float = MCP_GATEWAY_CALL_TIMEOUT_S) -> None:
        if not gateway_url:
            raise ValueError("GatewayRelay needs a gateway URL")
        self.upstream_url = gateway_url.rstrip("/")
        self.host_event = host_event
        self.stamp = {**identity_headers(user, tenant, session), **auth_headers(token)}
        self.client = httpx.Client(base_url=self.upstream_url,
                                   timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._lock = threading.Lock()
        self._calls: dict[object, HeldCall] = {}

    # -- the table
    def held(self, key, reattach: bool, body: bytes, headers: dict) -> HeldCall | None:
        """The entry to wait on: an existing one (joined or stored), a new
        one started now, or None for an unknown key on a re-attach."""
        with self._lock:
            entry = self._calls.get(key)
            if entry is not None:
                return entry
            if reattach:
                return None
            entry = self._calls[key] = HeldCall()
        threading.Thread(target=self._run, args=(entry, body, headers), daemon=True,
                         name=f"gateway-call:{key if isinstance(key, str) else '-'}").start()
        return entry

    def delivered(self, key, entry: HeldCall) -> None:
        with self._lock:
            if self._calls.get(key) is entry:
                del self._calls[key]

    def discard(self, call_id) -> None:
        """The guest declared the call lost (death resume), or left a request
        that cannot be re-attached: forget it. A gateway request still
        running finishes into nothing."""
        with self._lock:
            self._calls.pop(call_id, None)

    # -- the gateway
    def _run(self, entry: HeldCall, body: bytes, headers: dict) -> None:
        """One buffered POST /mcp into `entry`; a gateway that cannot be
        reached is a 502 and a gateway error event."""
        started = time.monotonic()
        try:
            resp = self.client.post(MCP_PATH, content=body, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("gateway %s unreachable: %s", self.upstream_url, e)
            self.record({"op": "error", "source": "forwarder", "ok": False,
                         "ms": elapsed_ms(started),
                         "error": f"gateway unreachable: {e}"[:300]})
            entry.finish(502, error_body(f"gateway unreachable: {e}", "gateway_unreachable"))
            return
        except Exception as e:           # never leave a joiner waiting
            logger.exception("gateway call failed")
            entry.finish(502, error_body(f"forwarder: {type(e).__name__}", "forwarder_error"))
            return
        entry.finish(resp.status_code, resp.content,
                     resp.headers.get("Content-Type", "application/json"),
                     {k: resp.headers[k] for k in _PASS_BACK if k in resp.headers})

    def record(self, body: dict) -> None:
        if self.host_event is not None:
            try:
                self.host_event("gateway", body)
            except Exception as e:  # the log never breaks the relay
                logger.warning("gateway event failed: %s", e)

    def close(self) -> None:
        self.client.close()


def _peer_gone(conn: socket.socket) -> bool:
    """`conn` is readable: has the guest closed its end (EOF)?"""
    try:
        return conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class _Handler(UDSHandler):
    server_version = "sb-gateway-forwarder/0.3"
    log = logger

    def _not_found(self) -> None:
        if self.command not in ("GET", "HEAD"):
            self._read_body()          # drain, keep-alive
        self._error(404, f"no route for {self.command} {self.path}", "not_found")

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_found

    def do_POST(self) -> None:
        body = self._read_body()       # always drain (keep-alive)
        if self.path != MCP_PATH:      # exact: no query, no other path
            self._error(404, f"no route for POST {self.path}", "not_found")
            return
        relay: GatewayRelay = self.owner.relay
        headers = {**guest_headers(self.headers), **relay.stamp}
        call_id = self.headers.get(CALL_HEADER)
        # no X-SB-Call: a key no guest can send, so never re-attachable
        key = call_id or object()
        reattach = bool(call_id) and (self.headers.get(REATTACH_HEADER) or "").strip() == "1"
        entry = relay.held(key, reattach, body, headers)
        if entry is None:
            self._error(410, "unknown call_id", "call_gone")
            return
        if not entry.wait(self.connection):
            self.close_connection = True
            if not call_id:
                relay.discard(key)                 # nobody can come back for it
            return
        try:
            self._reply(entry.status, entry.body, entry.content_type, entry.headers)
            self.wfile.flush()
        except OSError:
            self.close_connection = True
            if not call_id:
                relay.discard(key)
            return                                 # kept for the re-attach
        relay.delivered(key, entry)


class GatewayForwarder(UDSService):
    """The :5011 listener; the relay (and its held calls) belongs to the worker."""

    handler = _Handler
    name = "gateway-forwarder"

    def __init__(self, listener: socket.socket, relay: GatewayRelay) -> None:
        super().__init__(listener)
        self.relay = relay
