from pydantic import BaseModel, field_validator

from protocol.mcp import valid_upstream_name


class MCPServerConfig(BaseModel):
    """A remote MCP server (gateway upstream) that tenants reference by name.

    M4: agent-api only uses the name (and tool_defs_ttl_s for the legacy
    skill's cache); url, auth_token, timeout_s and allow_private belong to the
    mcp-gateway's gateway.json and are accepted here only so an existing
    tenants.json keeps loading (and can be migrated)."""
    name: str
    url: str = ""
    auth_token: str | None = None
    timeout_s: int = 300           # per-tool-call ceiling
    tool_defs_ttl_s: int = 300     # cached tools/list refresh interval
    allow_private: bool = False    # permit loopback/private-range URL (e.g. docker host)

    @field_validator("name")
    @classmethod
    def _valid_upstream_name(cls, v: str) -> str:
        if not valid_upstream_name(v):
            raise ValueError("MCP server name must be non-empty without '__' or ':'")
        return v


class Tenant(BaseModel):
    """One entry: an API key bound to origins, a prompt, and a toolset.

    `user` is the person owning this entry — several tenants may share one
    user. Sessions/traces are stored per tenant under the user's directory;
    user-scope memory is shared across the user's tenants. Defaults to the
    tenant name, so single-tenant users need not set it.
    """
    name: str
    user: str = ""
    api_key: str
    origins: list[str] = []
    system_prompt: str | None = None      # None -> global SYSTEM_PROMPT
    local_skills: list[str] | None = None  # None -> all local skills
    mcp_servers: list[str] = []            # names from the top-level mcp_servers map
    promoted: list[str] = []               # gateway-only (migrate_config): first-class MCP tools
    max_tools: int | None = None           # None -> config.MAX_TOOLS
    max_tokens: int | None = None          # None -> LLM default
    engine: str | None = None              # None -> config.AGENT_ENGINE; "legacy" | "harness"
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

    @field_validator("engine")
    @classmethod
    def _valid_engine(cls, v: str | None) -> str | None:
        if v is not None and v not in ("legacy", "harness"):
            raise ValueError("engine must be 'legacy' or 'harness'")
        return v

    def model_post_init(self, __context) -> None:
        if not self.user:
            self.user = self.name
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
        schedules): no key, no prompt, unset knobs left out; `engine` is
        the effective one (AGENT_ENGINE when unset: schedules skip legacy)."""
        from app.config import AGENT_ENGINE
        from flows.activities import SESSION_CONFIG_KEYS
        entry = self.model_dump(exclude_none=True,
                                include={"name", "user", "schedules", *SESSION_CONFIG_KEYS})
        entry["engine"] = self.engine or AGENT_ENGINE
        return entry

    def allowed_skill_names(self) -> set[str] | None:
        """Skill names this tenant may use; None means unrestricted (default tenant)."""
        if self.local_skills is None and not self.mcp_servers:
            return None
        local = set(self.local_skills or [])
        return local | {f"mcp_{s}" for s in self.mcp_servers}
