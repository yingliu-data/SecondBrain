import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth.guest import (
    GuestSessionManager,
    MAX_SESSIONS_PER_IP_PER_HOUR,
    client_ip,
)
from app.routes import guest as guest_route


ALLOWED_ORIGIN = "http://localhost:5173"


def make_request(headers: dict | None = None, client: tuple | None = ("10.0.0.1", 1234)) -> Request:
    """Build a bare Starlette Request with the given headers / socket peer."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "client": client})


# ── client_ip resolution ────────────────────────────────


def test_client_ip_prefers_cf_connecting_ip():
    req = make_request({
        "CF-Connecting-IP": "203.0.113.7",
        "X-Forwarded-For": "1.2.3.4, 5.6.7.8",
    })
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_first_forwarded_for():
    req = make_request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
    assert client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_to_socket_peer():
    req = make_request({}, client=("192.0.2.9", 5555))
    assert client_ip(req) == "192.0.2.9"


def test_client_ip_unknown_when_nothing_available():
    req = make_request({}, client=None)
    assert client_ip(req) == "unknown"


def test_client_ip_ignores_blank_headers():
    req = make_request({"CF-Connecting-IP": "  ", "X-Forwarded-For": " , 5.6.7.8"},
                       client=("192.0.2.9", 5555))
    assert client_ip(req) == "192.0.2.9"


# ── rate-limit bucketing through the endpoint ───────────


class _StubRegistry:
    """No avatar_control skill — the generator short-circuits before any LLM call."""
    _skills: dict = {}


@pytest.fixture
def guest_client(monkeypatch):
    manager = GuestSessionManager()
    monkeypatch.setattr(guest_route, "guest_manager", manager)
    guest_route.set_dependencies(_StubRegistry(), None)

    app = FastAPI()
    app.include_router(guest_route.router)
    return TestClient(app)


def post_guest(client: TestClient, session_id: str, ip: str | None = None, origin=ALLOWED_ORIGIN):
    headers = {"Origin": origin} if origin is not None else {}
    if ip:
        headers["CF-Connecting-IP"] = ip
    return client.post(
        "/api/v1/guest/chat",
        json={"session_id": session_id, "message": "wave"},
        headers=headers,
    )


def test_rate_limit_buckets_are_per_client_ip(guest_client):
    """IP A exhausting its quota must not lock out IP B.

    Regression test: the endpoint used to key on request.client.host, which is
    the proxy's address for every visitor, making this one global bucket.
    """
    for i in range(MAX_SESSIONS_PER_IP_PER_HOUR):
        r = post_guest(guest_client, f"a-{i}", ip="203.0.113.1")
        assert r.status_code == 200, r.text

    over = post_guest(guest_client, "a-over", ip="203.0.113.1")
    assert over.status_code == 429

    other = post_guest(guest_client, "b-0", ip="198.51.100.2")
    assert other.status_code == 200, other.text


def test_rate_limit_still_enforced_within_one_ip(guest_client):
    for i in range(MAX_SESSIONS_PER_IP_PER_HOUR):
        assert post_guest(guest_client, f"c-{i}", ip="203.0.113.3").status_code == 200
    r = post_guest(guest_client, "c-over", ip="203.0.113.3")
    assert r.status_code == 429
    assert "Rate limit" in r.json()["error"]


# ── origin check ────────────────────────────────────────


def test_disallowed_origin_rejected_and_logged(guest_client, caplog):
    with caplog.at_level("WARNING", logger="security"):
        r = post_guest(guest_client, "d-0", ip="203.0.113.4", origin="https://evil.example")
    assert r.status_code == 403
    assert any(
        "GUEST_ORIGIN_REJECT" in rec.message and "203.0.113.4" in rec.message
        for rec in caplog.records
    )


def test_allowed_origin_accepted(guest_client):
    assert post_guest(guest_client, "e-0", ip="203.0.113.5").status_code == 200
