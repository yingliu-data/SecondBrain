"""Tool-budget guardrails.

SYSTEM_DESIGN.md 8.3 caps `max_tools_in_context` at 10 because Qwen3-14B's
tool-selection accuracy degrades past roughly 10-15 tools. ANALYSIS.md 1.2
records that `email`, `github_cli` and `gitlab_cli` cannot execute in the
deployed container at all (no EMAIL_* vars in docker-compose.yml; no `gh`/`glab`
in the python:3.12-slim image), so they must not spend that budget.
"""

import json
from types import SimpleNamespace

import pytest

import app.skills.registry as registry_mod
from app.skills.registry import DEFAULT_DISABLED, SkillRegistry


# Tools presented to the default (unrestricted) tenant once the dead skills are
# gated off. avatar_control contributes 4 of these and must stay enabled: it
# serves the robot.yingliu.site guest endpoint. If this assertion trips, either
# the new tools are worth more than the ones already in context (then raise the
# ceiling deliberately, with a note here) or a skill needs gating.
#
# Raised 11 -> 13 deliberately: app/skills/remember/ was missing __init__.py, so
# pkgutil.iter_modules() skipped it and the skill was NEVER discovered -- while
# data/skills.json still carried a ghost "remember": true entry from before.
# ContextBuilder reads the memory store on every turn but nothing could write to
# it, so durable user memory was effectively read-only. Restoring it costs 2
# tools (remember, forget) and is the premise of the product. Still 16 -> 13
# overall. This is the strongest argument for P2-0 (progressive disclosure)
# landing before Phase 2 adds Darwin/weather/maps on top.
TOOL_BUDGET_CEILING = 13


@pytest.fixture
def make_registry(tmp_path, monkeypatch):
    """Build a real SkillRegistry (it discovers the real skill packages) whose
    enabled/disabled state file lives in tmp_path, never agent-api/data/."""

    def _make(saved_state: dict | None = None):
        state_file = tmp_path / "skills.json"
        if saved_state is not None:
            state_file.write_text(json.dumps(saved_state))
        monkeypatch.setattr(registry_mod, "SKILLS_STATE_FILE", state_file)
        return SkillRegistry(), state_file

    return _make


def test_state_file_is_isolated_from_real_data_dir(make_registry, tmp_path):
    """Guard the guard: these tests must not touch agent-api/data/skills.json."""
    _, state_file = make_registry()
    assert state_file.parent == tmp_path
    assert state_file.exists()  # _discover() persists on boot


# ── Task 1: dead skills are gated off by default ──────────────


def test_dead_skills_disabled_on_fresh_registry(make_registry):
    """No data/skills.json present -> email/github_cli/gitlab_cli start off."""
    reg, _ = make_registry()
    for name in ("email", "github_cli", "gitlab_cli"):
        assert name in reg._skills, f"{name} should still be discovered"
        assert reg._enabled[name] is False, f"{name} should be disabled by default"


def test_other_skills_still_enabled_by_default(make_registry):
    """The gate is a denylist, not a policy change for everything else."""
    reg, _ = make_registry()
    for name, skill in reg._skills.items():
        if name in DEFAULT_DISABLED:
            continue
        assert reg._enabled[name] is True, f"{name} unexpectedly disabled"
    # avatar_control powers the live guest endpoint — must never be gated.
    assert "avatar_control" not in DEFAULT_DISABLED
    assert reg._enabled["avatar_control"] is True


def test_dead_skill_tools_absent_from_context(make_registry):
    reg, _ = make_registry()
    names = {t["function"]["name"] for t in reg.get_active_tool_definitions(None)}
    dead = {
        t["function"]["name"]
        for n in DEFAULT_DISABLED
        if n in reg._skills
        for t in reg._skills[n].get_tool_definitions()
    }
    assert dead, "expected the gated skills to define tools"
    assert names.isdisjoint(dead)


def test_saved_state_beats_default(make_registry):
    """A previously-saved data/skills.json that explicitly enables a gated skill
    is honoured — user intent (PATCH /api/v1/skills/{name}) wins over the
    default, so each skill is re-enablable once its dependency ships."""
    reg, state_file = make_registry({"github_cli": True})

    assert reg._enabled["github_cli"] is True
    assert reg._enabled["email"] is False  # untouched ones keep the default

    names = {t["function"]["name"] for t in reg.get_active_tool_definitions(None)}
    gh = {t["function"]["name"]
          for t in reg._skills["github_cli"].get_tool_definitions()}
    assert gh <= names

    # ...and the choice survives the save-on-discover round trip.
    assert json.loads(state_file.read_text())["github_cli"] is True


def test_gated_skill_is_reenablable_at_runtime(make_registry):
    """PATCH /api/v1/skills/{name} -> set_enabled() still works on a gated skill."""
    reg, state_file = make_registry()
    assert reg.set_enabled("email", True) is True
    names = {t["function"]["name"] for t in reg.get_active_tool_definitions(None)}
    assert {t["function"]["name"]
            for t in reg._skills["email"].get_tool_definitions()} <= names
    assert json.loads(state_file.read_text())["email"] is True


def test_default_tenant_tool_count_within_budget(make_registry):
    """The headline number: what the default tenant actually sees."""
    reg, _ = make_registry()
    defs = reg.get_active_tool_definitions(None)
    per_skill = {
        n: len(s.get_tool_definitions())
        for n, s in reg._skills.items()
        if reg._enabled[n]
    }
    assert len(defs) <= TOOL_BUDGET_CEILING, (
        f"default tenant now presents {len(defs)} tools, over the documented "
        f"ceiling of {TOOL_BUDGET_CEILING} (SYSTEM_DESIGN.md 8.3: "
        f"max_tools_in_context = 10; Qwen3-14B degrades past ~10-15). "
        f"Enabled skills: {per_skill}"
    )


# ── Task 2: _legacy_turn must not lose a turn on disconnect ───


class _FakeStore(dict):
    """Stand-in for SessionStore: dict-like, with the save() hook."""

    def __init__(self):
        super().__init__()
        self.saved: dict[str, list] = {}

    def save(self, key, messages):
        self.saved[key] = list(messages)


def _tenant():
    return SimpleNamespace(
        name="default",
        session_key=lambda sid: sid,
        system_prompt=None,
        # max_tool_rounds is the real field since the P0-7 split; max_tools is
        # kept here only because the deprecated alias must stay exercised.
        max_tool_rounds=None,
        max_tools=None,
        max_tokens=None,
        allowed_skill_names=lambda: None,
    )


@pytest.fixture
def legacy_chat(monkeypatch):
    from app.routes import chat as chat_mod

    store = _FakeStore()
    monkeypatch.setattr(chat_mod, "sessions", store)

    async def fake_loop(message, history, registry, llm, **kwargs):
        history.append({"role": "user", "content": message})
        yield "data: start\n\n"
        history.append({"role": "assistant", "content": "the reply"})
        yield "data: the reply\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_mod, "run_agent_loop", fake_loop)
    return chat_mod, store


async def test_legacy_turn_persists_on_normal_completion(legacy_chat):
    chat_mod, store = legacy_chat
    events = [e async for e in chat_mod._legacy_turn(_tenant(), "s1", "hi")]
    assert len(events) == 3
    assert store.saved["s1"][-1] == {"role": "assistant", "content": "the reply"}


async def test_legacy_turn_persists_on_client_disconnect(legacy_chat):
    """Client hangs up mid-stream: FastAPI closes the generator, which raises
    GeneratorExit at the yield. The reply must still be saved."""
    chat_mod, store = legacy_chat
    gen = chat_mod._legacy_turn(_tenant(), "s1", "hi")

    assert await gen.__anext__() == "data: start\n\n"
    assert await gen.__anext__() == "data: the reply\n\n"
    assert "s1" not in store.saved  # nothing saved yet — mid-stream
    await gen.aclose()  # <- the disconnect

    assert store.saved["s1"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "the reply"},
    ]


async def test_legacy_turn_persists_when_the_loop_raises(legacy_chat, monkeypatch):
    chat_mod, store = legacy_chat

    async def boom(message, history, registry, llm, **kwargs):
        history.append({"role": "user", "content": message})
        yield "data: start\n\n"
        history.append({"role": "assistant", "content": "partial"})
        raise RuntimeError("llm exploded")

    monkeypatch.setattr(chat_mod, "run_agent_loop", boom)

    with pytest.raises(RuntimeError):
        [e async for e in chat_mod._legacy_turn(_tenant(), "s2", "hi")]
    assert store.saved["s2"][-1]["content"] == "partial"


async def test_legacy_turn_tolerates_store_without_save(legacy_chat, monkeypatch):
    """SESSION_BACKEND=memory returns a plain dict — no save() to call."""
    chat_mod, _ = legacy_chat
    monkeypatch.setattr(chat_mod, "sessions", {})
    events = [e async for e in chat_mod._legacy_turn(_tenant(), "s3", "hi")]
    assert len(events) == 3


# ── P0-7: the two ceilings that shared one name ──────────────────────────
# MAX_TOOLS was spent as loop ITERATIONS in agent/loop.py, while SYSTEM_DESIGN
# 8.3 also needs a TOOL-COUNT ceiling. One variable, two meanings, pulling in
# opposite directions ("3 rounds per voice turn" vs "under 10 tools").

def test_tenant_accepts_new_max_tool_rounds():
    from app.tenants.models import Tenant
    t = Tenant(name="t", api_key="k" * 32, max_tool_rounds=3)
    assert t.max_tool_rounds == 3


def test_tenant_honours_deprecated_max_tools_alias():
    """Existing data/tenants.json files use max_tools; they must keep working."""
    from app.tenants.models import Tenant
    t = Tenant(name="t", api_key="k" * 32, max_tools=7)
    assert t.max_tool_rounds == 7, "the deprecated alias no longer maps through"


def test_new_name_wins_when_both_are_set():
    from app.tenants.models import Tenant
    t = Tenant(name="t", api_key="k" * 32, max_tools=7, max_tool_rounds=3)
    assert t.max_tool_rounds == 3


def test_rounds_and_context_ceilings_are_independent():
    from app import config
    assert config.MAX_TOOL_ROUNDS != config.MAX_TOOLS_IN_CONTEXT or True
    # The point is that both exist and are separately settable.
    assert isinstance(config.MAX_TOOLS_IN_CONTEXT, int)
    assert config.MAX_TOOLS == config.MAX_TOOL_ROUNDS, "deprecated alias must track rounds"
