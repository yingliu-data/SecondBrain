from abc import ABC, abstractmethod


class BaseSkill(ABC):
    """Base class for all skills. Each skill is a self-contained capability."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier, e.g. 'web_search', 'calendar'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name shown in the iPhone skills manager."""

    @property
    @abstractmethod
    def description(self) -> str:
        """What this skill does, shown in the iPhone skills manager."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Semver string, e.g. '1.0.0'."""

    @property
    @abstractmethod
    def execution_side(self) -> str:
        """'server' — runs on the server. 'device' — delegates to iPhone."""

    @property
    def keywords(self) -> list[str]:
        """Trigger words for keyword-based tool discovery (Stage 1).
        Override to list words that indicate this skill is relevant.
        Used by SkillRegistry.get_tools_for_query() for fast pre-filtering."""
        return []

    @property
    def always_available(self) -> bool:
        """If True, this skill's tools are always included in the LLM context
        regardless of query matching. Use sparingly — only for skills that
        could be relevant to any query (e.g., web_search)."""
        return False

    @abstractmethod
    def get_tool_definitions(self) -> list[dict]:
        """Return OpenAI-compatible tool definitions for this skill.
        These get passed to llama-server in the tools parameter."""

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """Execute a server-side tool. Only called for server skills.
        Device skills are delegated to the iPhone automatically."""
        raise NotImplementedError(f"{self.name} is a device skill — execution happens on the iPhone.")
