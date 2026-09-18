"""Golden test for the SSE wire contract with IndexApp.

Why byte-level: the event framing is parsed by a Swift client in a separate
repo. A changed key order, an added field, or a lost trailing space in a token
frame is invisible in a Python diff and breaks the client silently. This test
pins the exact bytes so a refactor of the emission sites -- replacing hand-built
f-strings with HarnessEvent.to_sse() -- has to prove it changed nothing.

If you are deliberately changing the wire format, this test SHOULD fail, and
the fix is a coordinated IndexApp release, not an edit to the expected values.
"""

import json

from sb_contracts.models import (
    AvatarEvent,
    DoneEvent,
    TokenEvent,
    ToolCallEvent,
)


# The exact frames the pre-refactor loop emitted, transcribed from the
# f-strings at loop.py:111,112,217,231 as of commit 7621b9f.
def _legacy_token(word: str) -> str:
    return f"event: token\ndata: {json.dumps({'text': word + ' '})}\n\n"


def _legacy_done() -> str:
    return "event: done\ndata: {}\n\n"


def _legacy_avatar(tool_name: str, parsed: object) -> str:
    return f"event: avatar_command\ndata: {json.dumps({'name': tool_name, 'result': parsed})}\n\n"


def _legacy_tool_call(tc_id: str, tool_name: str, arguments: dict) -> str:
    return f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': tool_name, 'arguments': arguments})}\n\n"


def test_token_frame_is_byte_identical():
    for word in ["Hello", "", "with spaces", 'quote"inside', "unicode-é", "12:34"]:
        assert TokenEvent.of(word + " ").to_sse() == _legacy_token(word), word


def test_done_frame_is_byte_identical():
    assert DoneEvent.of().to_sse() == _legacy_done()


def test_avatar_frame_is_byte_identical():
    for result in [{"frames": [1, 2]}, {"status": "ok"}, [], {"nested": {"a": 1}}]:
        assert AvatarEvent.of("set_pose", result).to_sse() == _legacy_avatar("set_pose", result)


def test_tool_call_frame_is_byte_identical():
    cases = [
        ("tc_1", "get_weather", {"location": "London"}),
        ("tc_2", "create_event", {}),
        ("tc_3", "x", {"b": 2, "a": 1}),  # key order must be preserved as given
    ]
    for tc_id, name, args in cases:
        assert ToolCallEvent.of(tc_id, name, args).to_sse() == _legacy_tool_call(tc_id, name, args)


def test_frames_end_with_a_blank_line():
    """SSE requires the double newline terminator; a client blocks without it."""
    for ev in [TokenEvent.of("x "), DoneEvent.of(),
               AvatarEvent.of("set_pose", {}), ToolCallEvent.of("i", "n", {})]:
        assert ev.to_sse().endswith("\n\n")


def test_token_frame_preserves_the_trailing_space():
    """The loop streams `word + " "`; IndexApp relies on that spacing to
    reassemble the sentence. A helper that strips it would silently run every
    word together."""
    assert TokenEvent.of("Hello ").to_sse() == 'event: token\ndata: {"text": "Hello "}\n\n'


# ── PA-5: ChatHarness is a typed entry point, not a behaviour change ─────

async def test_chat_harness_yields_the_same_frames_as_the_raw_loop():
    """The adapter must not alter the wire output -- that is its whole claim."""
    from sb_contracts.models import TurnRequest
    from app.agent.harness import ChatHarness
    from app.agent.loop import run_agent_loop

    class Registry:
        def get_tools_for_query(self, q, allowed=None): return []
        def get_server_tool_names(self, allowed=None): return set()
        def get_device_tool_names(self, allowed=None): return set()

    class LLM:
        async def chat_completion(self, messages, **kw):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "Hi there"}}]}

    direct = [f async for f in run_agent_loop("hello", [], Registry(), LLM())]

    harness = ChatHarness(Registry(), LLM())
    turn = TurnRequest(session_id="s", tenant="t", user="u", text="hello")
    via_harness = [e.to_sse() async for e in harness.run(turn)]

    assert via_harness == direct


def test_chat_harness_does_not_claim_turn_control_it_lacks():
    """abort()/steer() are Phase 6. ChatHarness must implement Harness, not
    InteractiveHarness, until they exist -- an interface that advertises
    unimplemented capability is worse than a narrow one."""
    from sb_contracts.interfaces import Harness, InteractiveHarness
    from app.agent.harness import ChatHarness

    assert issubclass(ChatHarness, Harness)
    assert not issubclass(ChatHarness, InteractiveHarness)
