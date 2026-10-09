"""gateway_forwarder — :5011 → mcp-gateway (PLAN.md 8–9, M4-CONTRACTS §1–2, §3.3).

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
run 45 minutes). A gateway that cannot be reached is a 502 JSON body and a
`gateway{op: error, source: forwarder}` host event (the gateway cannot log
its own outage, PLAN.md 14).

Plain requests (no `X-SB-Call`: tools/list, ...) are relayed on the guest's
connection; a `text/event-stream` answer is relayed as it arrives.

Held calls (§3.3: long calls survive a park). A request with `X-SB-Call:
<call_id>` runs in its own thread, independent of the guest's connection; the
answer (buffered) is kept in the worker-lived GatewayRelay table
`{call_id: running | done(status, body)}`:
  - the guest's connection drops (park, snapshot, link loss): the gateway
    request keeps running, its answer is stored
  - the same call_id again (with or without `X-SB-Reattach: 1`): joins the
    running call, or gets the stored answer at once — the gateway sees one
    request
  - an entry is dropped once its answer was written to a guest; a later
    first send (no X-SB-Reattach) with that call_id is a new request (the
    `sb/input_response` re-send, §3.2)
  - an unknown call_id with `X-SB-Reattach: 1` → 410, never re-executed
  - the worker discards the entry of a call the guest declared `lost`
The relay outlives the forwarder: closing :5011 around a park stops
accepting, never a held call.

  GatewayRelay(gateway_url, user=, tenant=, session=, host_event=None,
               token=INTERNAL_TOKEN, timeout_s=MCP_GATEWAY_CALL_TIMEOUT_S)
    .discard(call_id) .pending() .close()
  GatewayForwarder(listener, relay)   .start() / .stop(join=True)
"""
from __future__ import annotations

import logging
import select
import socket
import threading
import time
from typing import Callable

import httpx

from app.config import INTERNAL_TOKEN, MCP_GATEWAY_CALL_TIMEOUT_S
from app.gateway.client import auth_headers, identity_headers
from app.host.uds_http import UDSHandler, UDSService

logger = logging.getLogger("host.gateway_forwarder")

MCP_PATH = "/mcp"
CALL_HEADER = "X-SB-Call"
REATTACH_HEADER = "X-SB-Reattach"
# never forwarded from the guest (hop-by-hop, recomputed, or the host's to set)
_DROP = {"host", "content-length", "connection", "keep-alive", "proxy-connection",
         "proxy-authorization", "proxy-authenticate", "te", "trailer", "trailers",
         "transfer-encoding", "upgrade", "authorization"}
# gateway response headers passed back besides Content-Type
_PASS_BACK = ("mcp-session-id", "mcp-protocol-version")
GONE_BODY = b'{"error": {"message": "unknown call_id", "type": "call_gone"}}'


def guest_headers(headers) -> dict:
    """The guest's headers minus hop-by-hop, Authorization and any X-SB-*
    except X-SB-Call."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _DROP or (lk.startswith("x-sb-") and lk != "x-sb-call"):
            continue
        out[k] = v
    return out


class HeldCall:
    """One X-SB-Call request: running until `done` is set, then the answer."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.status = 0
        self.body = b""
        self.content_type = "application/json"
        self.headers: dict[str, str] = {}

    def finish(self, status: int, body: bytes, content_type: str, headers: dict) -> None:
        self.status, self.body, self.content_type, self.headers = status, body, content_type, headers
        self.done.set()


class GatewayRelay:
    """The worker-lived half: the gateway client, the identity stamp and the
    held-call table. Survives the :5011 listener closing around a park."""

    def __init__(self, gateway_url: str, *, user: str, tenant: str, session: str,
                 host_event: Callable[[str, dict], object] | None = None,
                 token: str = INTERNAL_TOKEN,
                 timeout_s: float = MCP_GATEWAY_CALL_TIMEOUT_S) -> None:
        self.upstream_url = (gateway_url or "").rstrip("/")
        self.host_event = host_event
        self.stamp = {**identity_headers(user, tenant, session), **auth_headers(token)}
        self.client = httpx.Client(base_url=self.upstream_url or "http://unset.invalid",
                                   timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._lock = threading.Lock()
        self._calls: dict[str, HeldCall] = {}
        self.requests = 0               # gateway requests sent (tests)

    # -- the table
    def held(self, call_id: str, reattach: bool, body: bytes, headers: dict) -> HeldCall | None:
        """The entry to wait on: an existing one (joined or stored), a new
        one started now, or None for an unknown call_id on a re-attach."""
        with self._lock:
            entry = self._calls.get(call_id)
            if entry is not None:
                return entry
            if reattach:
                return None
            entry = self._calls[call_id] = HeldCall()
        threading.Thread(target=self._run, args=(entry, body, headers), daemon=True,
                         name=f"gateway-call:{call_id}").start()
        return entry

    def delivered(self, call_id: str, entry: HeldCall) -> None:
        with self._lock:
            if self._calls.get(call_id) is entry:
                del self._calls[call_id]

    def discard(self, call_id: str) -> None:
        """The guest declared the call lost (death resume): forget it. A
        gateway request still running finishes into nothing."""
        with self._lock:
            self._calls.pop(call_id, None)

    def pending(self) -> dict[str, str]:
        with self._lock:
            return {cid: "done" if e.done.is_set() else "running" for cid, e in self._calls.items()}

    def _run(self, entry: HeldCall, body: bytes, headers: dict) -> None:
        try:
            resp = self.post(body, headers)
            if resp is None:
                entry.finish(502, b'{"error": {"message": "gateway unreachable", '
                                  b'"type": "gateway_unreachable"}}', "application/json", {})
                return
            entry.finish(resp.status_code, resp.content,
                         resp.headers.get("Content-Type", "application/json"), _pass_back(resp))
        except Exception as e:           # never leave a joiner waiting
            logger.exception("held gateway call failed")
            entry.finish(502, f'{{"error": {{"message": "forwarder: {type(e).__name__}"}}}}'.encode(),
                         "application/json", {})

    # -- the gateway
    def post(self, body: bytes, headers: dict) -> httpx.Response | None:
        """One buffered POST /mcp; None (and a gateway error event) when the
        gateway cannot be reached."""
        started = time.monotonic()
        self.requests += 1
        try:
            return self.client.post(MCP_PATH, content=body, headers=headers)
        except httpx.HTTPError as e:
            self.unreachable(e, started)
            return None

    def unreachable(self, e: Exception, started: float) -> None:
        logger.warning("gateway %s unreachable: %s", self.upstream_url, e)
        self.record({"op": "error", "source": "forwarder", "ok": False,
                     "ms": int((time.monotonic() - started) * 1000),
                     "error": f"gateway unreachable: {e}"[:300]})

    def record(self, body: dict) -> None:
        if self.host_event is not None:
            try:
                self.host_event("gateway", body)
            except Exception as e:  # the log never breaks the relay
                logger.warning("gateway event failed: %s", e)

    def close(self) -> None:
        self.client.close()


def _pass_back(resp: httpx.Response) -> dict:
    return {k: resp.headers[k] for k in _PASS_BACK if k in resp.headers}


def _peer_gone(conn: socket.socket) -> bool:
    """The guest closed its end (EOF readable) while we wait for a call."""
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        if not readable:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class _Handler(UDSHandler):
    server_version = "sb-gateway-forwarder/0.2"
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
        if not relay.upstream_url:
            self._error(502, "no MCP gateway configured", "gateway_unavailable")
            return
        headers = {**guest_headers(self.headers), **relay.stamp}
        call_id = self.headers.get(CALL_HEADER)
        if call_id:
            self._held(relay, call_id, body, headers)
        else:
            self._plain(relay, body, headers)

    def _held(self, relay: GatewayRelay, call_id: str, body: bytes, headers: dict) -> None:
        reattach = (self.headers.get(REATTACH_HEADER) or "").strip() == "1"
        entry = relay.held(call_id, reattach, body, headers)
        if entry is None:
            self._reply(410, GONE_BODY, "application/json")
            return
        while not entry.done.wait(0.5):
            if _peer_gone(self.connection):
                self.close_connection = True       # the call runs on; a re-attach gets it
                return
        try:
            self._reply(entry.status, entry.body, entry.content_type, entry.headers)
            self.wfile.flush()
        except OSError:
            self.close_connection = True
            return                                 # kept for the re-attach
        relay.delivered(call_id, entry)

    def _plain(self, relay: GatewayRelay, body: bytes, headers: dict) -> None:
        started = time.monotonic()
        relay.requests += 1
        try:
            with relay.client.stream("POST", MCP_PATH, content=body, headers=headers) as resp:
                content_type = resp.headers.get("Content-Type", "application/json")
                if "text/event-stream" in content_type.lower():
                    try:
                        self._relay_sse(resp, content_type, _pass_back(resp))
                    except (httpx.HTTPError, OSError) as e:   # gateway cut, or the guest left
                        logger.warning("gateway stream ended: %s", e)
                    return
                self._reply(resp.status_code, resp.read(), content_type, _pass_back(resp))
        except httpx.HTTPError as e:
            relay.unreachable(e, started)
            self._error(502, f"gateway unreachable: {e}", "gateway_unreachable")


class GatewayForwarder(UDSService):
    """The :5011 listener; the relay (and its held calls) belongs to the worker."""

    handler = _Handler
    name = "gateway-forwarder"

    def __init__(self, listener: socket.socket, relay: GatewayRelay) -> None:
        super().__init__(listener)
        self.relay = relay
