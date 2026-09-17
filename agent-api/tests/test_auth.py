import hashlib
import hmac
import time

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import middleware
from app.auth.middleware import verify
from app.tenants import Tenant


def make_app(tenant_registry):
    middleware.set_tenant_registry(tenant_registry)
    app = FastAPI()

    @app.post("/probe")
    async def probe(tenant: Tenant = Depends(verify)):
        return {"tenant": tenant.name}

    # Second path + second method on the same path, so signature binding to
    # path and method can be exercised.
    @app.post("/other")
    async def other(tenant: Tenant = Depends(verify)):
        return {"tenant": tenant.name}

    @app.put("/probe")
    async def probe_put(tenant: Tenant = Depends(verify)):
        return {"tenant": tenant.name}

    return TestClient(app)


def signed_headers(key: str, body: str | bytes, ts: int | None = None) -> dict:
    """Legacy (v1) signing: HMAC over ts + body, no method/path binding."""
    ts = ts if ts is not None else int(time.time())
    raw = body.encode() if isinstance(body, str) else body
    sig = hmac.new(key.encode(), str(ts).encode() + raw, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"Bearer {key}",
        "X-Timestamp": str(ts),
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_headers_v2(
    key: str,
    body: str | bytes,
    method: str = "POST",
    path: str = "/probe",
    ts: int | None = None,
    query: str = "",
) -> dict:
    """v2 signing: METHOD \n PATH \n QUERY \n TS \n + raw body bytes."""
    ts = ts if ts is not None else int(time.time())
    raw = body.encode() if isinstance(body, str) else body
    msg = f"{method}\n{path}\n{query}\n{ts}\n".encode() + raw
    sig = hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"Bearer {key}",
        "X-Timestamp": str(ts),
        "X-Signature": sig,
        "X-Sig-Version": "2",
        "Content-Type": "application/json",
    }


BODY = '{"message":"hi"}'


def test_valid_tenant_auth(tenant_registry):
    client = make_app(tenant_registry)
    r = client.post("/probe", content=BODY, headers=signed_headers("wcc-key", BODY))
    assert r.status_code == 200
    assert r.json() == {"tenant": "wcc-events"}


def test_legacy_default_key_still_works(tenant_registry):
    client = make_app(tenant_registry)
    r = client.post("/probe", content=BODY, headers=signed_headers("test-default-key", BODY))
    assert r.status_code == 200
    assert r.json() == {"tenant": "default"}


def test_unknown_key_rejected(tenant_registry):
    client = make_app(tenant_registry)
    r = client.post("/probe", content=BODY, headers=signed_headers("nope", BODY))
    assert r.status_code == 401


def test_cross_tenant_signature_rejected(tenant_registry):
    """Bearer of tenant A + HMAC computed with tenant B's key must fail."""
    client = make_app(tenant_registry)
    headers = signed_headers("test-default-key", BODY)
    headers["Authorization"] = "Bearer wcc-key"
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 401


def test_stale_timestamp_rejected(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY, ts=int(time.time()) - 3600)
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 401


def test_tampered_body_rejected(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY)
    r = client.post("/probe", content='{"message":"evil"}', headers=headers)
    assert r.status_code == 401


# ── Signature versioning: v1 (legacy) stays accepted, v2 binds method+path ──


def test_v1_signature_still_accepted_without_version_header(tenant_registry):
    """Backwards compatibility: the shipped iOS client sends no X-Sig-Version."""
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY)
    assert "X-Sig-Version" not in headers
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"tenant": "wcc-events"}


def test_v1_signature_accepted_with_explicit_version_1(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY) | {"X-Sig-Version": "1"}
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 200


def test_v1_acceptance_logs_legacy_warning(tenant_registry, caplog):
    client = make_app(tenant_registry)
    with caplog.at_level("WARNING", logger="security"):
        r = client.post("/probe", content=BODY, headers=signed_headers("wcc-key", BODY))
    assert r.status_code == 200
    assert "AUTH_WARN legacy_signature tenant=wcc-events" in caplog.text


def test_v2_signature_accepted(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers_v2("wcc-key", BODY, "POST", "/probe")
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"tenant": "wcc-events"}


def test_v2_signature_does_not_replay_across_paths(tenant_registry):
    """Same key, body and timestamp signed for /probe must not work on /other."""
    client = make_app(tenant_registry)
    headers = signed_headers_v2("wcc-key", BODY, "POST", "/probe")
    assert client.post("/probe", content=BODY, headers=headers).status_code == 200
    r = client.post("/other", content=BODY, headers=headers)
    assert r.status_code == 401


def test_v2_signature_does_not_replay_across_methods(tenant_registry):
    """A POST signature must not authenticate the same path under PUT."""
    client = make_app(tenant_registry)
    headers = signed_headers_v2("wcc-key", BODY, "POST", "/probe")
    r = client.request("PUT", "/probe", content=BODY, headers=headers)
    assert r.status_code == 401
    # ...and signing for PUT makes it work.
    ok = signed_headers_v2("wcc-key", BODY, "PUT", "/probe")
    assert client.request("PUT", "/probe", content=BODY, headers=ok).status_code == 200


def test_v1_signature_rejected_when_v2_is_claimed(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY) | {"X-Sig-Version": "2"}
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 401


def test_unknown_signature_version_rejected(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY) | {"X-Sig-Version": "99"}
    r = client.post("/probe", content=BODY, headers=headers)
    assert r.status_code == 401


# ── Non-UTF-8 bodies must not crash the auth middleware ──


NON_UTF8 = b"\xff\xfe"


def test_non_utf8_body_with_bad_signature_is_401_not_500(tenant_registry):
    client = make_app(tenant_registry)
    headers = signed_headers("wcc-key", BODY)  # signature over a different body
    r = client.post("/probe", content=NON_UTF8, headers=headers)
    assert r.status_code == 401


def test_non_utf8_body_signed_correctly_authenticates(tenant_registry):
    """Raw-byte HMAC: a valid signature over non-UTF-8 bytes still verifies."""
    client = make_app(tenant_registry)
    r = client.post("/probe", content=NON_UTF8, headers=signed_headers("wcc-key", NON_UTF8))
    assert r.status_code == 200
    r2 = client.post(
        "/probe",
        content=NON_UTF8,
        headers=signed_headers_v2("wcc-key", NON_UTF8, "POST", "/probe"),
    )
    assert r2.status_code == 200


# ── Review F9(b): the query string must be covered ───────────────────────
# Without it, ?limit=200 and ?limit=999999 share a signature. The session and
# trace routes take exactly those parameters, so an attacker who captured one
# signed request could widen its result set arbitrarily within the 300s window.

def test_v2_signature_covers_the_query_string(tenant_registry):
    client = make_app(tenant_registry)
    ts = int(time.time())
    h = signed_headers_v2("wcc-key", BODY, path="/probe", query="limit=1", ts=ts)
    assert client.post("/probe?limit=1", content=BODY, headers=h).status_code == 200
    # Same signature, different query -> must be rejected.
    assert client.post("/probe?limit=999999", content=BODY, headers=h).status_code == 401


def test_v2_signature_with_no_query_still_works(tenant_registry):
    client = make_app(tenant_registry)
    h = signed_headers_v2("wcc-key", BODY, path="/probe")
    assert client.post("/probe", content=BODY, headers=h).status_code == 200
