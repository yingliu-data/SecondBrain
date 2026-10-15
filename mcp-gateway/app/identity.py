"""Identity headers and the internal bearer (protocol.mcp.bearer_ok, the
rule agent-api checks its side with)."""
from __future__ import annotations

from collections.abc import Mapping

from protocol.mcp import IDENTITY_HEADERS, bearer_ok, is_safe_id
from starlette.requests import Request

from app.pins import Identity


def identity_from_headers(headers: Mapping[str, str]) -> tuple[Identity | None, str]:
    """(Identity, "") or (None, reason). `headers` keys are lower-case. The
    values become pin-file path segments, so each must pass `is_safe_id`
    (the rule agent-api uses for the same ids)."""
    values = []
    for h in IDENTITY_HEADERS:
        v = headers.get(h.lower())
        if not v:
            return None, f"missing {h} header"
        if not is_safe_id(v):
            return None, f"invalid {h} header"
        values.append(v)
    return Identity(*values), ""


def request_identity(request: Request) -> Identity:
    """The Identity `main.AuthMiddleware` validated and stored for this request."""
    return request.state.identity


__all__ = ["bearer_ok", "identity_from_headers", "request_identity"]
