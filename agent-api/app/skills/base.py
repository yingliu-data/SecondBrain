from abc import ABC, abstractmethod
from contextvars import ContextVar

from sb_contracts.enums import ExecutionMode, ExecutionSide

# Per-request context so skills (e.g. remember) can find the active session
# and user without racing on shared singleton state. Set by routes/chat.py.
_current_session: ContextVar = ContextVar("current_session", default=None)
_current_user_id: ContextVar = ContextVar("current_user_id", default="")


def set_current_context(session, user_id: str):
    _current_session.set(session)
    _current_user_id.set(user_id)


def get_current_session():
    return _current_session.get()


def get_current_user_id() -> str:
    return _current_user_id.get()


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
    def execution_side(self) -> ExecutionSide:
        """Where this skill's tools run.

        ExecutionSide is a StrEnum, so subclasses returning the bare strings
        "server" / "device" remain correct -- the enum compares equal to them.
        """

    @property
    def execution_mode(self) -> ExecutionMode:
        """Whether this skill's tools may run concurrently with batch siblings.

        SEQUENTIAL by default, which is the safe answer: device tools must not
        raise two EventKit prompts at once, and anything with a side effect
        should not race a sibling. A read-only lookup can opt into PARALLEL.
        """
        return (ExecutionMode.SEQUENTIAL
                if self.execution_side == ExecutionSide.DEVICE
                else ExecutionMode.PARALLEL)

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

    def set_llm(self, llm):
        """Optional: give this skill access to the LLM provider.
        Override in skills that need to make secondary LLM calls."""
        pass

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """Execute a server-side tool. Only called for server skills.
        Device skills are delegated to the iPhone automatically."""
        raise NotImplementedError(f"{self.name} is a device skill — execution happens on the iPhone.")
