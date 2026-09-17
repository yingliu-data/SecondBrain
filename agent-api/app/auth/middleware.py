import time, hmac, hashlib, logging
from fastapi import Request, HTTPException
from app.tenants import Tenant, TenantRegistry

sec_log = logging.getLogger("security")

# Injected at startup (see main.py)
_tenants: TenantRegistry | None = None

# ── Request signing schemes ───────────────────────────────────
#
# v1 (legacy): HMAC-SHA256 over  ts_bytes + raw_body
# v2 (current): HMAC-SHA256 over  method + "\n" + path + "\n" + ts + "\n" + raw_body
#
# v1 does not bind a signature to the HTTP method or the request path, so a
# captured request body signed for one endpoint is a valid signature for any
# other endpoint accepting the same body inside the ±300s timestamp window.
# v2 closes that replay hole. Clients select the scheme with the
# `X-Sig-Version` header (absent or "1" => v1, "2" => v2).
#
# DEPRECATION: v1 acceptance exists only so the shipped IndexApp (iOS) client
# keeps working during rollout. It is scheduled for removal one release after
# IndexApp ships v2 signing. Every accepted v1 signature logs
# `AUTH_WARN legacy_signature ...` to the `security` logger, so once those log
# lines stop appearing in production it is safe to drop v1. Removing it is a
# one-line change: delete the `if version == "1":` branch in `_signature_ok()`
# (and then the now-unused `_expected_v1` helper).


def set_tenant_registry(registry: TenantRegistry):
    global _tenants
    _tenants = registry


def _expected_v1(key: str, ts: str, body: bytes) -> str:
    """Legacy scheme: HMAC over the timestamp bytes followed by the raw body.

    The body is never decoded — for UTF-8 bodies this is byte-for-byte the same
    message the old `f"{ts}{body.decode()}".encode()` produced, so existing
    clients are unaffected; for non-UTF-8 bodies it no longer raises
    UnicodeDecodeError (which surfaced as a 500 from an unauthenticated route).
    """
    return hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()


def _expected_v2(key: str, method: str, path: str, ts: str, body: bytes) -> str:
    """Current scheme: signature is bound to the method and path as well."""
    prefix = f"{method}\n{path}\n{ts}\n".encode()
    return hmac.new(key.encode(), prefix + body, hashlib.sha256).hexdigest()


def _signature_ok(request: Request, tenant: Tenant, ts: str, body: bytes, sig: str, ip: str) -> bool:
    version = request.headers.get("X-Sig-Version", "1") or "1"
    if version == "2":
        expected = _expected_v2(
            tenant.api_key, request.method, request.url.path, ts, body
        )
        return hmac.compare_digest(sig, expected)
    if version == "1":
        if hmac.compare_digest(sig, _expected_v1(tenant.api_key, ts, body)):
            sec_log.warning(f"AUTH_WARN legacy_signature tenant={tenant.name} ip={ip}")
            return True
        return False
    return False


async def verify(request: Request) -> Tenant:
    """4-layer auth: Cloudflare Access (network) + Bearer + HMAC + Timestamp.

    The bearer token identifies the tenant; the HMAC signature must be
    computed with that same tenant's key. Returns the resolved Tenant.
    """
    ip = request.client.host if request.client else "unknown"

    auth = request.headers.get("Authorization", "")
    tenant = _tenants.get_by_api_key(auth[7:]) if auth.startswith("Bearer ") else None
    if tenant is None:
        sec_log.warning(f"AUTH_FAIL ip={ip} reason=bad_token")
        raise HTTPException(401, "Unauthorized")

    ts = request.headers.get("X-Timestamp", "")
    try:
        if abs(time.time() - int(ts)) > 300:
            raise ValueError
    except (ValueError, TypeError):
        sec_log.warning(f"AUTH_FAIL ip={ip} tenant={tenant.name} reason=bad_timestamp")
        raise HTTPException(401, "Unauthorized")

    body = await request.body()
    sig = request.headers.get("X-Signature", "")
    if not _signature_ok(request, tenant, ts, body, sig, ip):
        sec_log.warning(f"AUTH_FAIL ip={ip} tenant={tenant.name} reason=bad_hmac")
        raise HTTPException(401, "Unauthorized")

    # Origin sanity check (warn-only): browsers enforce CORS, this just logs
    # a mismatch between the tenant and the claimed origin.
    origin = request.headers.get("Origin")
    if origin and tenant.origins and origin not in tenant.origins:
        sec_log.warning(f"AUTH_WARN ip={ip} tenant={tenant.name} reason=origin_mismatch origin={origin}")

    request.state.tenant = tenant
    return tenant
