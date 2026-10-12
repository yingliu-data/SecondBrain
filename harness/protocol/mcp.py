"""mcp — wire constants and names shared by the guest, the host forwarder, the
gateway and agent-api. Stdlib only, pure data.

One definition each, so a rename breaks every side's unit tests instead of
only the end-to-end run.
"""
from __future__ import annotations

import hmac
import re

# --- tool names: {upstream}__{tool} -----------------------------------------
NAMESPACE_SEP = "__"
META_TOOLS = ("search_tools", "describe_tool", "call_tool")
CALL_TOOL = "call_tool"


def namespaced(upstream: str, tool: str) -> str:
    return f"{upstream}{NAMESPACE_SEP}{tool}"


def split_namespaced(name: str) -> tuple[str, str]:
    """'wcc__draft_event' -> ('wcc', 'draft_event'). Tool names may contain '__'
    themselves, so split on the FIRST separator (upstream names never do)."""
    upstream, _, tool = name.partition(NAMESPACE_SEP)
    return upstream, tool


def valid_upstream_name(name) -> bool:
    return isinstance(name, str) and bool(name) and NAMESPACE_SEP not in name and ":" not in name


# --- input required relay ----------------------------------------------------
INPUT_REQUIRED = "sb/input_required"
INPUT_RESPONSE = "sb/input_response"
ACTIONS = ("accept", "decline", "cancel")

# --- forwarder-held calls -----------------------------------------------------
CALL_HEADER = "X-SB-Call"
REATTACH_HEADER = "X-SB-Reattach"

# --- identity -----------------------------------------------------------------
USER_HEADER = "X-SB-User"
TENANT_HEADER = "X-SB-Tenant"
SESSION_HEADER = "X-SB-Session"
IDENTITY_HEADERS = (USER_HEADER, TENANT_HEADER, SESSION_HEADER)

# The same rule agent-api uses for user and session ids (app.session.ids): the
# values become path segments on both sides, so both sides check one rule.
_SAFE_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")


def is_safe_id(value) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


# --- the internal bearer (agent-api <-> mcp-gateway, both directions) ---------
def bearer_header(token: str) -> dict:
    """{"Authorization": "Bearer <token>"}, or {} when the token is empty."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def bearer_ok(header, token: str) -> bool:
    """An incoming Authorization header is exactly "Bearer <token>"
    (constant time). An empty token means no check (dev only)."""
    if not token:
        return True
    return hmac.compare_digest((header or "").encode(), f"Bearer {token}".encode())


# --- internal routes ------------------------------------------------------------
GATEWAY_EVENTS_PATH = "/internal/gateway-events"   # agent-api: the gateway's events in
INTERNAL_TOOLS_PATH = "/internal/tools"            # mcp-gateway: a session's tool surface

# the identity keys of a gateway event body (the rest is the event itself)
GATEWAY_EVENT_IDENTITY = ("user", "tenant", "session_id")


def gateway_event_identity(user: str, tenant: str, session: str) -> dict:
    return dict(zip(GATEWAY_EVENT_IDENTITY, (user, tenant, session)))
