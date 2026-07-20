import pytest

from app.session.store import SessionStore
from app.tenants import Tenant


def test_store_isolation_between_tenants(tmp_path):
    store = SessionStore(str(tmp_path / "conv.db"))
    store.save("abc", [{"role": "user", "content": "default tenant"}])
    store.save("wcc-events:abc", [{"role": "user", "content": "wcc tenant"}])
    assert store.get("abc")[0]["content"] == "default tenant"
    assert store.get("wcc-events:abc")[0]["content"] == "wcc tenant"
    ids = {row["session_id"] for row in store.list_sessions()}
    assert ids == {"abc", "wcc-events:abc"}


# ── Agent loop with fakes ─────────────────────────────────────

class FakeRegistry:
    def __init__(self):
        self.seen_allowed = "unset"

    def get_tools_for_query(self, query, allowed=None):
        self.seen_allowed = allowed
        return []

    def get_server_tool_names(self, allowed=None):
        return set()

    def get_device_tool_names(self, allowed=None):
        return set()


class FakeLLM:
    def __init__(self):
        self.seen = []

    async def chat_completion(self, messages, tools=None, max_tokens=None,
                              chat_template_kwargs=None):
        self.seen.append({"messages": messages, "max_tokens": max_tokens})
        return {"choices": [{"message": {"content": "final answer"},
                             "finish_reason": "stop"}]}


async def collect(gen):
    return [event async for event in gen]


@pytest.mark.asyncio
async def test_tenant_prompt_and_budget_threaded():
    from app.agent.loop import run_agent_loop
    registry, llm = FakeRegistry(), FakeLLM()
    events = await collect(run_agent_loop(
        "hello", [], registry, llm,
        system_prompt="TENANT PROMPT {current_time}",
        max_tokens=777,
        allowed_skills={"web_search"},
    ))
    assert registry.seen_allowed == {"web_search"}
    system = llm.seen[0]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("TENANT PROMPT ")
    assert llm.seen[0]["max_tokens"] == 777
    assert any("event: done" in e for e in events)


@pytest.mark.asyncio
async def test_legacy_defaults_unchanged():
    from app.agent.loop import run_agent_loop
    from app.config import SYSTEM_PROMPT
    registry, llm = FakeRegistry(), FakeLLM()
    await collect(run_agent_loop("hello", [], registry, llm))
    assert registry.seen_allowed is None
    prefix = SYSTEM_PROMPT.split("{current_time}")[0]
    assert llm.seen[0]["messages"][0]["content"].startswith(prefix)
