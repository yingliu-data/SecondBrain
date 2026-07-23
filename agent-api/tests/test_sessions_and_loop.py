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


# ── End-of-turn outcome summaries ─────────────────────────────

class ToolLoopRegistry(FakeRegistry):
    """Offers one server tool and records executions."""

    def __init__(self, result="tool says hi"):
        super().__init__()
        self.result = result
        self.calls = []

    def get_tools_for_query(self, query, allowed=None):
        self.seen_allowed = allowed
        return [{"type": "function", "function": {"name": "mytool"}}]

    def get_server_tool_names(self, allowed=None):
        return {"mytool"}

    async def execute_server_tool(self, name, arguments, allowed=None):
        self.calls.append(name)
        return self.result


class AlwaysToolLLM:
    """Returns a tool call whenever tools are offered; text otherwise."""

    def __init__(self, wrap_up_text="wrap-up summary", fail_wrap_up=False):
        self.wrap_up_text = wrap_up_text
        self.fail_wrap_up = fail_wrap_up
        self.seen = []

    async def chat_completion(self, messages, tools=None, max_tokens=None,
                              chat_template_kwargs=None):
        self.seen.append({"messages": messages, "tools": tools})
        if tools:
            return {"choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{"id": "t1", "function": {
                        "name": "mytool", "arguments": "{}"}}],
                },
                "finish_reason": "tool_calls"}]}
        if self.fail_wrap_up:
            raise RuntimeError("wrap-up boom")
        return {"choices": [{"message": {"content": self.wrap_up_text},
                             "finish_reason": "stop"}]}


@pytest.mark.asyncio
async def test_exhausted_loop_streams_wrap_up_summary():
    from app.agent.loop import run_agent_loop
    registry, llm = ToolLoopRegistry(), AlwaysToolLLM()
    history = []
    events = await collect(run_agent_loop(
        "do it", history, registry, llm, max_tools=2))
    assert len(registry.calls) == 2  # budget respected
    # Final no-tools call produced the user-facing summary (streamed word-wise)
    assert llm.seen[-1]["tools"] is None
    joined = "".join(events)
    assert "wrap-up" in joined and "summary" in joined
    assert any("event: done" in e for e in events)
    assert history[-1] == {"role": "assistant", "content": "wrap-up summary"}


@pytest.mark.asyncio
async def test_exhausted_loop_falls_back_to_tool_outcome_line():
    from app.agent.loop import run_agent_loop
    registry = ToolLoopRegistry()
    llm = AlwaysToolLLM(fail_wrap_up=True)
    history = []
    events = await collect(run_agent_loop(
        "do it", history, registry, llm, max_tools=1))
    final = history[-1]["content"]
    assert "mytool (ok)" in final
    assert any("event: done" in e for e in events)


class FailsAfterFirstLLM(AlwaysToolLLM):
    """One tool round, then the LLM goes down."""

    async def chat_completion(self, messages, tools=None, max_tokens=None,
                              chat_template_kwargs=None):
        if self.seen:
            raise RuntimeError("llm down")
        return await super().chat_completion(
            messages, tools=tools, max_tokens=max_tokens,
            chat_template_kwargs=chat_template_kwargs)


@pytest.mark.asyncio
async def test_llm_error_mentions_completed_tools():
    from app.agent.loop import run_agent_loop
    registry, llm = ToolLoopRegistry(), FailsAfterFirstLLM()
    history = []
    events = await collect(run_agent_loop(
        "do it", history, registry, llm, max_tools=3))
    final = history[-1]["content"]
    assert "mytool (ok)" in final
    assert any("event: done" in e for e in events)


class EmptyFinalLLM(FakeLLM):
    async def chat_completion(self, messages, tools=None, max_tokens=None,
                              chat_template_kwargs=None):
        self.seen.append({"messages": messages})
        return {"choices": [{"message": {"content": None},
                             "finish_reason": "stop"}]}


@pytest.mark.asyncio
async def test_empty_final_content_still_yields_a_reply():
    from app.agent.loop import run_agent_loop
    registry, llm = FakeRegistry(), EmptyFinalLLM()
    history = []
    events = await collect(run_agent_loop("hello", history, registry, llm))
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"]  # never empty/None
    assert "No tools were run." in history[-1]["content"]
    assert any("event: done" in e for e in events)
