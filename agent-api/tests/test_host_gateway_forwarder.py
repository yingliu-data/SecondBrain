"""GatewayForwarder on the :5011 Unix socket against a fake gateway over TCP:
header stamping and stripping, POST /mcp only, streaming, outage, and the
held calls of M4-CONTRACTS §3.3 (survive a dropped guest and a park)."""
import json
import socket
import threading
import time

import httpx
import pytest

from app.host.endpoints import Endpoints
from app.host.gateway_forwarder import GatewayForwarder, GatewayRelay
from gateway_fakes import FakeGateway

CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "wcc-events__draft_event", "arguments": {}}}


@pytest.fixture
def fwd(tmp_path):
    gw = FakeGateway().start()
    ep = Endpoints(tmp_path, [5011])
    events: list = []
    relay = GatewayRelay(gw.url, user="u", tenant="t", session="sess_1",
                         host_event=lambda t, b: events.append((t, b)), token="tok", timeout_s=10)
    f = GatewayForwarder(ep.listener(5011), relay).start()
    client = httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5011))),
                          base_url="http://gateway", timeout=10)
    yield gw, relay, f, ep, client, events
    if gw.hold is not None:
        gw.hold.set()
    client.close()
    f.stop()
    relay.close()
    ep.close()
    gw.stop()


def test_stamps_identity_and_bearer_strips_guest_headers(fwd):
    gw, relay, f, ep, client, _ = fwd
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
                    headers={"X-SB-User": "evil", "x-sb-tenant": "other", "X-SB-Session": "x",
                             "X-SB-Reattach": "1", "Authorization": "Bearer guest",
                             "Accept": "application/json, text/event-stream",
                             "Mcp-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200
    assert r.json()["result"]["seen"]["method"] == "tools/list"
    assert r.headers["mcp-session-id"] == "none"
    h = gw.requests[0]["headers"]
    assert (h["x-sb-user"], h["x-sb-tenant"], h["x-sb-session"]) == ("u", "t", "sess_1")
    assert h["authorization"] == "Bearer tok"
    assert "x-sb-reattach" not in h and "x-sb-call" not in h
    assert h["accept"] == "application/json, text/event-stream"
    assert h["mcp-protocol-version"] == "2025-06-18"
    # keep-alive: a second request on the same connection
    assert client.post("/mcp", json=CALL).status_code == 200 and len(gw.requests) == 2


def test_only_post_mcp_is_forwarded(fwd):
    gw, relay, f, ep, client, _ = fwd
    assert client.get("/mcp").status_code == 404
    assert client.post("/internal/call", json={"name": "x"}).status_code == 404
    assert client.get("/internal/tools").status_code == 404
    assert client.post("/mcp?x=1", json={}).status_code == 404
    assert client.delete("/mcp").status_code == 404
    assert gw.requests == []


def test_status_and_body_verbatim_and_sse_streamed(fwd):
    gw, relay, f, ep, client, _ = fwd
    gw.status = 400
    r = client.post("/mcp", json={"id": 2})
    assert r.status_code == 400 and r.json()["id"] == 2
    gw.status, gw.sse = 200, True
    with client.stream("POST", "/mcp", json={"id": 3}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = r.read().decode()
    assert text.startswith("event: message\ndata: ") and '"id": 3' in text


def test_unreachable_gateway_is_502_and_a_host_event(tmp_path):
    ep = Endpoints(tmp_path, [5011])
    events: list = []
    relay = GatewayRelay("http://127.0.0.1:9", user="u", tenant="t", session="s",
                         host_event=lambda t, b: events.append((t, b)), timeout_s=2)
    f = GatewayForwarder(ep.listener(5011), relay).start()
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5011))),
                          base_url="http://g") as c:
            r = c.post("/mcp", json=CALL)
            assert r.status_code == 502 and r.json()["error"]["type"] == "gateway_unreachable"
            r = c.post("/mcp", json=CALL, headers={"X-SB-Call": "call_x"})
            assert r.status_code == 502
        assert [t for t, _ in events] == ["gateway", "gateway"]
        assert events[0][1]["op"] == "error" and events[0][1]["source"] == "forwarder"
    finally:
        f.stop()
        relay.close()
        ep.close()


# ---------------------------------------------------------------- held calls (§3.3)
def raw_post(path, headers: dict, body: dict) -> socket.socket:
    """Send one POST /mcp on a raw Unix socket and return it (unread)."""
    raw = json.dumps(body).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    lines = ["POST /mcp HTTP/1.1", "Host: gateway", "Content-Type: application/json",
             f"Content-Length: {len(raw)}"] + [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + raw)
    return s


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_guest_drops_mid_call_and_reattach_gets_the_stored_answer(fwd):
    gw, relay, f, ep, client, _ = fwd
    gw.hold = threading.Event()
    s = raw_post(ep.path(5011), {"X-SB-Call": "call_1"}, CALL)
    assert wait_for(lambda: len(gw.mcp_requests()) == 1)
    assert gw.mcp_requests()[0]["headers"]["x-sb-call"] == "call_1"
    s.close()                                      # the guest goes away (park, link loss)
    time.sleep(0.7)                                # the handler notices; the call runs on
    assert relay.pending() == {"call_1": "running"}
    gw.hold.set()                                  # the gateway answers later
    assert wait_for(lambda: relay.pending() == {"call_1": "done"})
    r = client.post("/mcp", json=CALL, headers={"X-SB-Call": "call_1", "X-SB-Reattach": "1"})
    assert r.status_code == 200 and r.json()["result"]["seen"]["id"] == 1
    assert len(gw.mcp_requests()) == 1             # never re-executed
    assert relay.pending() == {}                   # delivered: dropped
    # a re-attach after delivery: gone
    r = client.post("/mcp", json=CALL, headers={"X-SB-Call": "call_1", "X-SB-Reattach": "1"})
    assert r.status_code == 410 and len(gw.mcp_requests()) == 1


def test_unknown_call_on_reattach_is_410_and_not_forwarded(fwd):
    gw, relay, f, ep, client, _ = fwd
    r = client.post("/mcp", json=CALL, headers={"X-SB-Call": "ghost", "X-SB-Reattach": "1"})
    assert r.status_code == 410 and gw.requests == []


def test_reattach_joins_a_running_call_and_a_new_send_after_delivery_is_new(fwd):
    gw, relay, f, ep, client, _ = fwd
    gw.hold = threading.Event()
    s = raw_post(ep.path(5011), {"X-SB-Call": "call_2"}, CALL)
    assert wait_for(lambda: len(gw.mcp_requests()) == 1)
    s.close()
    out: dict = {}

    def reattach():
        out["r"] = client.post("/mcp", json=CALL,
                               headers={"X-SB-Call": "call_2", "X-SB-Reattach": "1"})

    t = threading.Thread(target=reattach)
    t.start()
    time.sleep(0.3)
    assert "r" not in out                          # joined, waiting
    gw.hold.set()
    t.join(5)
    assert out["r"].status_code == 200 and len(gw.mcp_requests()) == 1
    # the input_response re-send: same call_id, no X-SB-Reattach, after delivery
    resend = dict(CALL, id=2)
    r = client.post("/mcp", json=resend, headers={"X-SB-Call": "call_2"})
    assert r.status_code == 200 and r.json()["result"]["seen"]["id"] == 2
    assert len(gw.mcp_requests()) == 2


def test_held_call_survives_the_listener_closing_around_a_park(fwd):
    gw, relay, f, ep, client, _ = fwd
    gw.hold = threading.Event()
    s = raw_post(ep.path(5011), {"X-SB-Call": "call_3"}, CALL)
    assert wait_for(lambda: len(gw.mcp_requests()) == 1)
    s.close()
    f.stop(join=False)                             # park: the port closes
    ep.close_port(5011)
    gw.hold.set()
    assert wait_for(lambda: relay.pending() == {"call_3": "done"})
    f2 = GatewayForwarder(ep.reopen(5011), relay).start()   # restore: a new listener
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(ep.path(5011))),
                          base_url="http://g", timeout=5) as c:
            r = c.post("/mcp", json=CALL, headers={"X-SB-Call": "call_3", "X-SB-Reattach": "1"})
        assert r.status_code == 200 and len(gw.mcp_requests()) == 1
    finally:
        f2.stop()


def test_discarded_call_is_gone(fwd):
    gw, relay, f, ep, client, _ = fwd
    gw.hold = threading.Event()
    s = raw_post(ep.path(5011), {"X-SB-Call": "call_4"}, CALL)
    assert wait_for(lambda: len(gw.mcp_requests()) == 1)
    s.close()
    relay.discard("call_4")                        # the guest declared it lost
    gw.hold.set()
    r = client.post("/mcp", json=CALL, headers={"X-SB-Call": "call_4", "X-SB-Reattach": "1"})
    assert r.status_code == 410 and len(gw.mcp_requests()) == 1
