import pytest

from app.skills.base import BaseSkill
from app.skills.registry import SkillRegistry


class FakeSkill(BaseSkill):
    def __init__(self, name, side="server", tools=None, always=False):
        self._name, self._side = name, side
        self._tools = tools or [f"{name}_tool"]
        self._always = always
        self.calls = []

    name = property(lambda self: self._name)
    display_name = property(lambda self: self._name)
    description = property(lambda self: "fake")
    version = property(lambda self: "0")
    execution_side = property(lambda self: self._side)
    always_available = property(lambda self: self._always)

    def get_tool_definitions(self):
        return [{"type": "function", "function": {"name": t, "description": "", "parameters": {}}}
                for t in self._tools]

    async def execute(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return f"ok:{tool_name}"


@pytest.fixture
def registry():
    reg = SkillRegistry.__new__(SkillRegistry)  # skip fs discovery/state
    reg._skills, reg._enabled = {}, {}
    reg.register(FakeSkill("web_search", always=True))
    reg.register(FakeSkill("avatar_control"))
    reg.register(FakeSkill("mcp_wcc", tools=["wcc__draft_event"], always=True))
    return reg


def test_unrestricted_sees_all(registry):
    names = registry.get_server_tool_names(None)
    assert names == {"web_search_tool", "avatar_control_tool", "wcc__draft_event"}


def test_allowed_filters_tools(registry):
    names = registry.get_server_tool_names({"web_search", "mcp_wcc"})
    assert names == {"web_search_tool", "wcc__draft_event"}
    defs = registry.get_active_tool_definitions({"web_search"})
    assert [d["function"]["name"] for d in defs] == ["web_search_tool"]


@pytest.mark.asyncio
async def test_execute_respects_allowed(registry):
    ok = await registry.execute_server_tool("avatar_control_tool", {}, None)
    assert ok == "ok:avatar_control_tool"
    denied = await registry.execute_server_tool("avatar_control_tool", {}, {"web_search"})
    assert denied.startswith("Error: No skill provides")


def test_unregister(registry):
    registry.unregister("mcp_wcc")
    assert "wcc__draft_event" not in registry.get_server_tool_names(None)


def test_save_state_excludes_mcp(registry, tmp_path, monkeypatch):
    import app.skills.registry as mod
    monkeypatch.setattr(mod, "SKILLS_STATE_FILE", tmp_path / "skills.json")
    registry.set_enabled("web_search", False)
    import json
    saved = json.loads((tmp_path / "skills.json").read_text())
    assert "mcp_wcc" not in saved
    assert saved["web_search"] is False
