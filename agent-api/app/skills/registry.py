import importlib, pkgutil, json, logging
from pathlib import Path
from .base import BaseSkill

logger = logging.getLogger("skills")
SKILLS_STATE_FILE = Path("data/skills.json")


class SkillRegistry:
    """Discovers skills from the skills/ folder, manages enabled/disabled state."""

    def __init__(self):
        self._skills: dict[str, BaseSkill] = {}
        self._enabled: dict[str, bool] = {}
        self._load_state()
        self._discover()

    def _load_state(self):
        """Load enabled/disabled state from disk."""
        if SKILLS_STATE_FILE.exists():
            self._enabled = json.loads(SKILLS_STATE_FILE.read_text())
        else:
            self._enabled = {}

    def _save_state(self):
        """Persist enabled/disabled state to disk."""
        SKILLS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SKILLS_STATE_FILE.write_text(json.dumps(self._enabled, indent=2))

    def _discover(self):
        """Auto-discover all skill modules under app/skills/."""
        package = importlib.import_module("app.skills")
        for importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
            if not ispkg or modname in ("__pycache__",):
                continue
            try:
                mod = importlib.import_module(f"app.skills.{modname}.skill")
                for attr in dir(mod):
                    obj = getattr(mod, attr)
                    if (isinstance(obj, type) and issubclass(obj, BaseSkill)
                            and obj is not BaseSkill):
                        skill = obj()
                        self._skills[skill.name] = skill
                        if skill.name not in self._enabled:
                            self._enabled[skill.name] = True  # enabled by default
                        logger.info(f"Discovered skill: {skill.name} v{skill.version} ({skill.execution_side})")
            except Exception as e:
                logger.error(f"Failed to load skill '{modname}': {e}")
        self._save_state()

    # ── Public API ────────────────────────────────────────────

    def list_all(self) -> list[dict]:
        """List all skills with metadata. Used by /api/v1/skills GET."""
        return [
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": s.description,
                "version": s.version,
                "execution_side": s.execution_side,
                "enabled": self._enabled.get(s.name, True),
                "always_available": s.always_available,
                "keywords": s.keywords,
                "tools": [t["function"]["name"] for t in s.get_tool_definitions()],
            }
            for s in self._skills.values()
        ]

    def set_enabled(self, skill_name: str, enabled: bool) -> bool:
        """Enable or disable a skill. Returns False if skill not found."""
        if skill_name not in self._skills:
            return False
        self._enabled[skill_name] = enabled
        self._save_state()
        logger.info(f"Skill '{skill_name}' {'enabled' if enabled else 'disabled'}")
        return True

    def get_active_tool_definitions(self) -> list[dict]:
        """Return tool definitions for ALL enabled skills (fallback path).
        Prefer get_tools_for_query() to filter by relevance."""
        defs = []
        for name, skill in self._skills.items():
            if self._enabled.get(name, True):
                defs.extend(skill.get_tool_definitions())
        return defs

    def get_tools_for_query(self, query: str) -> list[dict]:
        """Return tool definitions relevant to a user query.

        Stage 1 (keyword pre-filter):
        - Check each skill's keywords against the lowercased query
        - Always include skills with always_available=True
        - If keyword matches found → return matched + always-available tools
        - If no matches → fall back to all enabled tools
        """
        query_lower = query.lower()
        matched = []
        always_on = []

        for name, skill in self._skills.items():
            if not self._enabled.get(name, True):
                continue
            if skill.always_available:
                always_on.extend(skill.get_tool_definitions())
            elif any(kw in query_lower for kw in skill.keywords):
                matched.extend(skill.get_tool_definitions())

        if matched:
            logger.info(f"Tool filter: {len(matched)} matched + {len(always_on)} always-on "
                        f"(vs {len(self.get_active_tool_definitions())} total)")
            return matched + always_on

        # No keyword hits — fall back to all enabled tools
        return self.get_active_tool_definitions()

    def get_server_tool_names(self) -> set[str]:
        """Names of all tools that execute on the server."""
        names = set()
        for name, skill in self._skills.items():
            if self._enabled.get(name, True) and skill.execution_side == "server":
                for t in skill.get_tool_definitions():
                    names.add(t["function"]["name"])
        return names

    def get_device_tool_names(self) -> set[str]:
        """Names of all tools that execute on the iPhone."""
        names = set()
        for name, skill in self._skills.items():
            if self._enabled.get(name, True) and skill.execution_side == "device":
                for t in skill.get_tool_definitions():
                    names.add(t["function"]["name"])
        return names

    async def execute_server_tool(self, tool_name: str, arguments: dict) -> str:
        """Route a server-side tool call to the right skill."""
        for skill in self._skills.values():
            if not self._enabled.get(skill.name, True):
                continue
            for t in skill.get_tool_definitions():
                if t["function"]["name"] == tool_name:
                    return await skill.execute(tool_name, arguments)
        return f"Error: No skill provides tool '{tool_name}'."
