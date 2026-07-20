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

    return TestClient(app)


def signed_headers(key: str, body: str, ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    sig = hmac.new(key.encode(), f"{ts}{body}".encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": f"Bearer {key}",
        "X-Timestamp": str(ts),
        "X-Signature": sig,
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
