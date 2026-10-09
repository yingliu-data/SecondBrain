import logging
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from protocol.allowlist import GUEST_BUILTINS

logger = logging.getLogger("tenants")


class Tenant(BaseModel):
    """One entry: an API key bound to origins, a prompt, and a toolset.

    `user` is the person owning this entry — several tenants may share one
    user. Sessions/traces are stored per tenant under the user's directory;
    user-scope memory is shared across the user's tenants. Defaults to the
    tenant name, so single-tenant users need not set it.

    `auth`: "key" (Bearer + HMAC with `api_key`, app/auth/middleware.py) or
    "origin" (no key; the browser Origin must be one of `origins` — the
    guest avatar page, routes/guest.py). An origin tenant is never matched
    by an API key.

    M6: every tenant runs the harness engine. An `engine` key is accepted
    and ignored with a warning; `mcp_servers` is accepted and ignored too
    (the mcp-gateway's gateway.json owns MCP access), except that a tenant
    listing MCP servers without `local_skills` still gets no local skills.
    """
    name: str
    user: str = ""
    api_key: str = ""
    auth: Literal["key", "origin"] = "key"
    origins: list[str] = []
    system_prompt: str | None = None      # None -> global SYSTEM_PROMPT
    local_skills: list[str] | None = None  # None -> all local skills
    mcp_servers: list[str] = []            # ignored by agent-api (the gateway's)
    promoted: list[str] = []               # gateway-only (migrate_config): first-class MCP tools
    max_tools: int | None = None           # None -> config.MAX_TOOLS
    max_tokens: int | None = None          # None -> LLM default
    exclude_builtins: list[str] = []       # guest built-ins this tenant's sessions do not get
    is_default: bool = False
    # M5 (Temporal session workflow): flows.SESSION_CONFIG_KEYS, read by
    # SessionConfig.from_tenant (None keeps the flows default);
    # `schedules` = [{cron, prompt, session, tz?}]
    wall_clock_s: int | None = None
    answer_wait_s: int | None = None       # parked question wait (phone 86400)
    max_tasks_per_run: int | None = None
    max_history_events: int | None = None  # history length before continue-as-new
    schedules: list[dict] = []

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if ":" in v or not v:
            raise ValueError("tenant name must be non-empty and must not contain ':'")
        return v

    @model_validator(mode="before")
    @classmethod
    def _ignore_engine(cls, data):
        if isinstance(data, dict) and "engine" in data:
            data = dict(data)
            engine = data.pop("engine")
            logger.warning("tenant %s: `engine: %s` ignored (every tenant runs the harness "
                           "engine since M6)", data.get("name"), engine)
        return data

    @field_validator("exclude_builtins")
    @classmethod
    def _known_builtins(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(GUEST_BUILTINS))
        if unknown:
            raise ValueError(f"exclude_builtins: not guest built-ins: {unknown}")
        return v

    def model_post_init(self, __context) -> None:
        if not self.user:
            self.user = self.name
        if self.auth == "key" and not self.api_key:
            raise ValueError(f"tenant {self.name!r}: api_key is required (auth: key)")
        from app.session.ids import is_safe_id
        if not is_safe_id(self.user) or not is_safe_id(self.name):
            raise ValueError(
                f"tenant name/user must be filesystem-safe [A-Za-z0-9_-]: "
                f"name={self.name!r} user={self.user!r}")

    def store_key(self, session_id: str) -> str:
        """Store key for this tenant. The default tenant keeps raw keys so
        existing conversation rows keep working."""
        if self.is_default:
            return session_id
        return f"{self.name}:{session_id}"

    def workflow_entry(self) -> dict:
        """The tenants.json view flows reads (SessionConfig.from_tenant,
        schedules): no key, no prompt, unset knobs left out."""
        from flows.activities import SESSION_CONFIG_KEYS
        return self.model_dump(exclude_none=True,
                               include={"name", "user", "schedules", *SESSION_CONFIG_KEYS})

    def allowed_skill_names(self) -> set[str] | None:
        """Local skill names this tenant may use; None means unrestricted
        (default tenant). MCP tools are the gateway's policy, not this."""
        if self.local_skills is None:
            return set() if self.mcp_servers else None
        return set(self.local_skills)
