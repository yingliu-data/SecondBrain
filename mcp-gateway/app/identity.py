"""Identity headers and the internal bearer (M4-CONTRACTS.md §1, §2)."""
from __future__ import annotations

import hmac
import re
from collections.abc import Mapping

from app.pins import Identity

H_USER, H_TENANT, H_SESSION = "x-sb-user", "x-sb-tenant", "x-sb-session"
_SAFE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")


def identity_from_headers(headers: Mapping[str, str]) -> tuple[Identity | None, str]:
    """(Identity, "") or (None, reason). Values become pin-file path
    segments, so anything outside [A-Za-z0-9._@-]{1,128}, '.' or '..' is refused."""
    values = []
    for h in (H_USER, H_TENANT, H_SESSION):
        v = headers.get(h)
        if not v:
            return None, f"missing {h} header"
        if not _SAFE.match(v) or v in (".", ".."):
            return None, f"invalid {h} header"
        values.append(v)
    return Identity(*values), ""


def bearer_ok(authorization: str | None, token: str) -> bool:
    if not token:
        return True  # dev only (§1)
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[len("Bearer "):].encode(), token.encode())
