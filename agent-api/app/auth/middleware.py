import time, hmac, hashlib, logging
from fastapi import Request, HTTPException
from app.config import API_SECRET_KEY

sec_log = logging.getLogger("security")


async def verify(request: Request):
    """4-layer auth: Cloudflare Access (network) + Bearer + HMAC + Timestamp."""
    ip = request.client.host if request.client else "unknown"

    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_SECRET_KEY}":
        sec_log.warning(f"AUTH_FAIL ip={ip} reason=bad_token")
        raise HTTPException(401, "Unauthorized")

    ts = request.headers.get("X-Timestamp", "")
    try:
        if abs(time.time() - int(ts)) > 300:
            raise ValueError
    except (ValueError, TypeError):
        sec_log.warning(f"AUTH_FAIL ip={ip} reason=bad_timestamp")
        raise HTTPException(401, "Unauthorized")

    body = await request.body()
    expected = hmac.new(
        API_SECRET_KEY.encode(),
        f"{ts}{body.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    sig = request.headers.get("X-Signature", "")
    if not hmac.compare_digest(sig, expected):
        sec_log.warning(f"AUTH_FAIL ip={ip} reason=bad_hmac")
        raise HTTPException(401, "Unauthorized")
