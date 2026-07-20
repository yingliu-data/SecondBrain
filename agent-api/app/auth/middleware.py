import time, hmac, hashlib, logging
from fastapi import Request, HTTPException
from app.tenants import Tenant, TenantRegistry

sec_log = logging.getLogger("security")

# Injected at startup (see main.py)
_tenants: TenantRegistry | None = None


def set_tenant_registry(registry: TenantRegistry):
    global _tenants
    _tenants = registry


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
    expected = hmac.new(
        tenant.api_key.encode(),
        f"{ts}{body.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    sig = request.headers.get("X-Signature", "")
    if not hmac.compare_digest(sig, expected):
        sec_log.warning(f"AUTH_FAIL ip={ip} tenant={tenant.name} reason=bad_hmac")
        raise HTTPException(401, "Unauthorized")

    # Origin sanity check (warn-only): browsers enforce CORS, this just logs
    # a mismatch between the tenant and the claimed origin.
    origin = request.headers.get("Origin")
    if origin and tenant.origins and origin not in tenant.origins:
        sec_log.warning(f"AUTH_WARN ip={ip} tenant={tenant.name} reason=origin_mismatch origin={origin}")

    request.state.tenant = tenant
    return tenant
