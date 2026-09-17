"""Correctness guards for the agent loop: truncated responses must not run
tools, normal tool calls still must, and a device-tool timeout must not leak
shared state."""

import json

import pytest


# ── Fakes (same shape as test_sessions_and_loop.py) ───────────

class ToolRegistry:
    """Offers one tool and records server-side executions."""

    def __init__(self, tool_name="mytool", device=False, result="tool says hi"):
        self.tool_name = tool_name
        self.device = device
        self.result = result
        self.calls = []

    def get_tools_for_query(self, query, allowed=None):
        return [{"type": "function", "function": {"name": self.tool_name}}]

    def get_server_tool_names(self, allowed=None):
        return set() if self.device else {self.tool_name}

    def get_device_tool_names(self, allowed=None):
        return {self.tool_name} if self.device else set()

    async def execute_server_tool(self, name, arguments, allowed=None):
        self.calls.append((name, arguments))
        return self.result


class ScriptedLLM:
    """First call (tools offered) returns `first`; later calls return text."""

    def __init__(self, first, text="all done"):
        self.first = first
        self.text = text
        self.seen = []

    async def chat_completion(self, messages, tools=None, max_tokens=None,
                              chat_template_kwargs=None):
        self.seen.append({"messages": messages, "tools": tools})
        if len(self.seen) == 1:
            return self.first
        return {"choices": [{"message": {"content": self.text},
                             "finish_reason": "stop"}]}


def truncated_tool_call_choice():
    """What vLLM returns when the token cap cuts a tool call in half: the
    arguments JSON is a fragment, so json.loads would fall back to {}."""
    return {"choices": [{
        "message": {
            "content": None,
            "tool_calls": [{"id": "t1", "function": {
                "name": "mytool",
                "arguments": '{"title": "Dentist", "start": "2026-0'}}],
        },
        "finish_reason": "length"}]}


def tool_call_choice():
    return {"choices": [{
        "message": {
            "content": None,
            "tool_calls": [{"id": "t1", "function": {
                "name": "mytool", "arguments": '{"title": "Dentist"}'}}],
        },
        "finish_reason": "tool_calls"}]}


async def collect(gen):
    return [event async for event in gen]


def streamed_text(events):
    return "".join(json.loads(e.split("data: ", 1)[1].split("\n\n")[0])["text"]
                   for e in events if e.startswith("event: token"))


# ── finish_reason="length" must not execute tools ─────────────

@pytest.mark.asyncio
async def test_truncated_tool_call_is_never_executed():
    from app.agent.loop import run_agent_loop
    registry = ToolRegistry()
    llm = ScriptedLLM(truncated_tool_call_choice(), text="that reply got cut off")
    traced = []
    history = []
    events = await collect(run_agent_loop(
        "book the dentist", history, registry, llm, max_tools=3,
        trace=lambda event, detail=None, duration_ms=None: traced.append(
            (event, detail))))

    # The half-formed call is dropped, not run with arguments={}.
    assert registry.calls == []
    assert ("truncated_response", {"loop": 1}) in traced
    # The user still gets a coherent turn, not a hang or an empty stream.
    assert any("event: done" in e for e in events)
    assert streamed_text(events).strip()
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"]


@pytest.mark.asyncio
async def test_truncated_response_falls_through_to_wrap_up():
    from app.agent.loop import run_agent_loop
    registry = ToolRegistry()
    llm = ScriptedLLM(truncated_tool_call_choice(), text="that reply got cut off")
    history = []
    events = await collect(run_agent_loop(
        "book the dentist", history, registry, llm, max_tools=3))
    # Exactly one retry, made with no tools offered (the shared wrap-up path).
    assert len(llm.seen) == 2
    assert llm.seen[-1]["tools"] is None
    assert "that reply got cut off" in streamed_text(events)
    assert history[-1] == {"role": "assistant",
                           "content": "that reply got cut off"}


@pytest.mark.asyncio
async def test_truncated_response_wrap_up_failure_still_answers():
    """Even if the wrap-up LLM call dies, the stream ends cleanly."""
    from app.agent.loop import run_agent_loop

    class DeadWrapUpLLM(ScriptedLLM):
        async def chat_completion(self, messages, tools=None, max_tokens=None,
                                  chat_template_kwargs=None):
            if self.seen:
                self.seen.append({"messages": messages, "tools": tools})
                raise RuntimeError("llm down")
            return await super().chat_completion(
                messages, tools=tools, max_tokens=max_tokens,
                chat_template_kwargs=chat_template_kwargs)

    registry = ToolRegistry()
    llm = DeadWrapUpLLM(truncated_tool_call_choice())
    history = []
    events = await collect(run_agent_loop(
        "book the dentist", history, registry, llm, max_tools=3))
    assert registry.calls == []
    assert "No tools were run." in history[-1]["content"]
    assert any("event: done" in e for e in events)


# ── guard against over-correction ─────────────────────────────

@pytest.mark.asyncio
async def test_normal_tool_call_still_executes():
    from app.agent.loop import run_agent_loop
    registry = ToolRegistry()
    llm = ScriptedLLM(tool_call_choice(), text="booked it")
    history = []
    events = await collect(run_agent_loop(
        "book the dentist", history, registry, llm, max_tools=3))
    assert registry.calls == [("mytool", {"title": "Dentist"})]
    assert "booked it" in streamed_text(events)
    assert any("event: done" in e for e in events)


# ── device-tool timeout must not leak shared state ────────────

@pytest.mark.asyncio
async def test_device_tool_timeout_leaves_no_shared_state(monkeypatch):
    from app.agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT", 0.05)
    loop_mod.tool_result_events.clear()
    loop_mod.tool_results.clear()

    registry = ToolRegistry(device=True)
    llm = ScriptedLLM(tool_call_choice(), text="the phone never answered")
    events = []
    async for event in loop_mod.run_agent_loop(
            "ask the phone", [], registry, llm, max_tools=3):
        events.append(event)
        if event.startswith("event: tool_call"):
            payload = json.loads(event.split("data: ", 1)[1].split("\n\n")[0])
            # The iPhone answers late: its result lands in the shared dict
            # around the moment the loop gives up waiting.
            loop_mod.tool_results[payload["id"]] = "late result"

    assert loop_mod.tool_result_events == {}
    assert loop_mod.tool_results == {}
    # The timeout was reported to the LLM as the tool's result, and the turn
    # still finished cleanly for the user.
    tool_msgs = [m for m in llm.seen[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "timed out" in tool_msgs[-1]["content"]
    assert any("event: done" in e for e in events)


# ── malformed tool arguments must fail closed ────────────────────────────
# Regression for the bug originally mis-attributed to finish_reason="length".
# The length path never reached tool execution at all. THIS is the path that ran
# tools with no arguments: vLLM emits finish_reason="tool_calls" carrying
# argument JSON the model truncated, json.loads raised, and the old code fell
# back to `arguments = {}` and executed the tool anyway -- a create_calendar_event
# with no title, no date, no calendar.


def _bad_args_response(raw_args, name="create_calendar_event"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "tc_bad", "type": "function",
                        "function": {"name": name, "arguments": raw_args}}]}}]}


class ArgRecordingRegistry:
    """Records anything that reaches execution. For bad args, nothing should."""

    def __init__(self):
        self.calls = []

    def get_tools_for_query(self, query, allowed=None):
        return [{"type": "function", "function": {"name": "create_calendar_event"}}]

    def get_server_tool_names(self, allowed=None):
        return {"create_calendar_event"}

    def get_device_tool_names(self, allowed=None):
        return set()

    async def execute_server_tool(self, name, args, allowed=None):
        self.calls.append((name, args))
        return "event created"


class TwoStepLLM:
    """One tool call with the given raw arguments, then a plain final answer."""

    def __init__(self, raw_args):
        self.raw_args = raw_args
        self.n = 0
        self.seen = None

    async def chat_completion(self, messages, **kwargs):
        self.n += 1
        if self.n == 1:
            return _bad_args_response(self.raw_args)
        self.seen = list(messages)
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "I could not complete that."}}]}


@pytest.mark.parametrize("raw_args", [
    '{"title": "Dentist", "start',   # truncated mid-string
    '{"title": ',                    # truncated mid-object
    "null",                          # valid JSON, not an object
    '"a string"',                    # valid JSON, not an object
    "[1, 2, 3]",                     # valid JSON, not an object
])
async def test_malformed_arguments_never_execute_the_tool(raw_args):
    from app.agent.loop import run_agent_loop

    reg, llm = ArgRecordingRegistry(), TwoStepLLM(raw_args)
    events = await collect(run_agent_loop("book me a dentist appointment", [], reg, llm))

    assert reg.calls == [], (
        f"tool executed as {reg.calls!r} despite unparseable argument JSON "
        f"{raw_args!r} -- this is the silent argument-less side effect")
    assert any("event: done" in e for e in events)

    tool_msgs = [m for m in (llm.seen or []) if m.get("role") == "tool"]
    assert tool_msgs, "the model was never told its tool call failed"
    assert "not valid JSON" in tool_msgs[0]["content"]
    assert "NOT run" in tool_msgs[0]["content"]


async def test_wellformed_arguments_still_execute():
    """Guard against over-correction: valid arguments must still run."""
    from app.agent.loop import run_agent_loop

    reg, llm = ArgRecordingRegistry(), TwoStepLLM('{"title": "Dentist"}')
    await collect(run_agent_loop("book it", [], reg, llm))
    assert reg.calls == [("create_calendar_event", {"title": "Dentist"})]
