from abc import ABC, abstractmethod
from contextvars import ContextVar

# Per-request context so skills (e.g. remember) can find the active session
# and user without racing on shared singleton state. Set by routes/chat.py.
_current_session: ContextVar = ContextVar("current_session", default=None)
_current_user_id: ContextVar = ContextVar("current_user_id", default="")
# The tenant and the session id the request runs under: the gateway skill
# stamps them as identity headers.
_current_tenant: ContextVar = ContextVar("current_tenant", default="")
_current_session_id: ContextVar = ContextVar("current_session_id", default="")


def set_current_context(session, user_id: str, tenant: str = "", session_id: str = ""):
    _current_session.set(session)
    _current_user_id.set(user_id)
    _current_tenant.set(tenant)
    _current_session_id.set(session_id)


def get_current_session():
    return _current_session.get()


def get_current_user_id() -> str:
    return _current_user_id.get()


def get_current_tenant() -> str:
    return _current_tenant.get()


def get_current_session_id() -> str:
    """The session id set with the context, else the session dir's own."""
    sid = _current_session_id.get()
    if not sid:
        session = _current_session.get()
        try:
            sid = session.read_meta().get("session_id", "") if session is not None else ""
        except (AttributeError, OSError, ValueError):
            sid = ""
    return sid


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
        """Words that indicate this skill is relevant, listed by GET /skills
        (the query prefilter that used them retired with the legacy loop)."""
        return []

    @property
    def always_available(self) -> bool:
        """Listed by GET /skills (the legacy loop always offered such a
        skill's tools; the harness offers every visible skill's)."""
        return False

    @abstractmethod
    def get_tool_definitions(self) -> list[dict]:
        """Return OpenAI-compatible tool definitions for this skill.
        These get passed to llama-server in the tools parameter."""

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """Execute a server-side tool. Only called for server skills.
        Device skills are delegated to the iPhone automatically."""
        raise NotImplementedError(f"{self.name} is a device skill — execution happens on the iPhone.")
