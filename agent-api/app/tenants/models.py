from pydantic import BaseModel, field_validator


class MCPServerConfig(BaseModel):
    """A remote MCP server that tenants can reference by name."""
    name: str
    url: str
    auth_token: str | None = None
    timeout_s: int = 300           # per-tool-call ceiling
    tool_defs_ttl_s: int = 300     # cached tools/list refresh interval
    allow_private: bool = False    # permit loopback/private-range URL (e.g. docker host)

    @field_validator("name")
    @classmethod
    def _no_namespace_separator(cls, v: str) -> str:
        if "__" in v or ":" in v:
            raise ValueError("MCP server name must not contain '__' or ':'")
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
    max_tools: int | None = None           # None -> config.MAX_TOOLS
    max_tokens: int | None = None          # None -> LLM default
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if ":" in v or not v:
            raise ValueError("tenant name must be non-empty and must not contain ':'")
        return v

    def model_post_init(self, __context) -> None:
        if not self.user:
            self.user = self.name
        from app.session.ids import is_safe_id
        if not is_safe_id(self.user) or not is_safe_id(self.name):
            raise ValueError(
                f"tenant name/user must be filesystem-safe [A-Za-z0-9_-]: "
                f"name={self.name!r} user={self.user!r}")

    def session_key(self, session_id: str) -> str:
        """Store key for this tenant. The default tenant keeps raw keys so
        existing conversation rows keep working."""
        if self.is_default:
            return session_id
        return f"{self.name}:{session_id}"

    def allowed_skill_names(self) -> set[str] | None:
        """Skill names this tenant may use; None means unrestricted (default tenant)."""
        if self.local_skills is None and not self.mcp_servers:
            return None
        local = set(self.local_skills or [])
        return local | {f"mcp_{s}" for s in self.mcp_servers}
