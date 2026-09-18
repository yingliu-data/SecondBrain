"""Tests for packages/sb-contracts.

They live here rather than in the package because this is the suite CI runs
(`cd agent-api && uv run pytest`). If the package ever grows its own runner,
move them.

The first test is the important one: it is the regression guard for a defect
this package briefly had, where two structurally identical ToolOutcome classes
existed and an `is` comparison silently reported a successful tool call as
failed.
"""

import pytest

from sb_contracts.enums import (
    CacheState,
    ExecutionSide,
    FinishReason,
    ThinkingLevel,
    ToolOutcome,
)
from sb_contracts.interfaces import Harness, InteractiveHarness, require_dev_opt_in
from sb_contracts.models import (
    CacheEntry,
    CompletionResponse,
    PolicyDecision,
    RawEvent,
    ToolDescriptor,
    ToolResult,
    Usage,
)


# ── The duplicate-enum regression ────────────────────────────────────────

def test_agent_enums_is_a_shim_not_a_second_definition():
    """app/agent/enums.py must RE-EXPORT the shared enums, never redefine them.

    Two StrEnum classes with the same members compare equal by value but not by
    identity, and ToolResult.ok uses `is`. A second definition therefore makes
    a successful tool call report ok == False, silently. This asserts identity,
    not equality -- equality would pass even with the bug present.
    """
    from app.agent import enums as agent_enums

    assert agent_enums.ToolOutcome is ToolOutcome
    assert agent_enums.FinishReason is FinishReason
    assert ToolResult(call_id="x", outcome=agent_enums.ToolOutcome.OK,
                      content="fine").ok is True


def test_strenum_members_are_plain_strings():
    """The property the whole adoption rests on: existing dict-shaped payloads
    and string comparisons keep working, so adopting these is not a migration."""
    assert FinishReason.LENGTH == "length"
    assert ExecutionSide.DEVICE == "device"
    assert ThinkingLevel.OFF == "off"
    assert f"{ToolOutcome.TIMEOUT}" == "timeout"


# ── ToolResult ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("outcome,expected", [
    (ToolOutcome.OK, True),
    (ToolOutcome.ERROR, False),
    (ToolOutcome.TIMEOUT, False),
    (ToolOutcome.INVALID_ARGUMENTS, False),
    (ToolOutcome.DENIED, False),
])
def test_tool_result_ok_tracks_outcome(outcome, expected):
    assert ToolResult(call_id="1", outcome=outcome, content="x").ok is expected


def test_tool_result_is_immutable():
    """Frozen so a result cannot be mutated between the sanitiser and the
    transcript -- the two places that read it are not adjacent."""
    r = ToolResult(call_id="1", outcome=ToolOutcome.OK, content="x")
    with pytest.raises(Exception):
        r.content = "tampered"


def test_tool_result_bytes_counts_utf8_not_characters():
    assert ToolResult(call_id="1", outcome=ToolOutcome.OK, content="é").bytes == 2


# ── CompletionResponse ───────────────────────────────────────────────────

def test_null_content_becomes_empty_string():
    """vLLM sends content: null. The key is present, so .get(k, "") yields None
    and None.split() kills the SSE stream mid-turn."""
    r = CompletionResponse.from_payload(
        {"choices": [{"finish_reason": "stop", "message": {"content": None}}]})
    assert r.content == ""


def test_unknown_finish_reason_degrades_to_error_not_crash():
    r = CompletionResponse.from_payload(
        {"choices": [{"finish_reason": "something_new", "message": {"content": "hi"}}]})
    assert r.finish_reason is FinishReason.ERROR


def test_missing_finish_reason_defaults_to_stop():
    r = CompletionResponse.from_payload({"choices": [{"message": {"content": "hi"}}]})
    assert r.finish_reason is FinishReason.STOP


def test_usage_is_captured_when_present_and_zero_when_absent():
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]}
    assert CompletionResponse.from_payload(payload).usage == Usage(0, 0, 0)
    payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    assert CompletionResponse.from_payload(payload).usage == Usage(10, 3, 13)


def test_tool_calls_default_to_empty_list_not_none():
    r = CompletionResponse.from_payload(
        {"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]})
    assert r.tool_calls == []


# ── ToolDescriptor ───────────────────────────────────────────────────────

def test_openai_round_trip_preserves_name_description_and_schema():
    d = ToolDescriptor(name="get_weather", description="Weather.",
                       parameters={"type": "object",
                                   "properties": {"location": {"type": "string"}}})
    assert ToolDescriptor.from_openai(d.to_openai()).parameters == d.parameters
    assert d.to_openai()["function"]["name"] == "get_weather"
    assert d.to_openai()["type"] == "function"


def test_descriptor_defaults_to_sequential_which_is_the_safe_answer():
    assert ToolDescriptor(name="x").mode == "sequential"
    assert ToolDescriptor(name="x").requires_confirmation is False


# ── Cache ────────────────────────────────────────────────────────────────

def test_cache_entry_goes_stale_at_its_ttl_boundary():
    e = CacheEntry(value="board", fetched_at=1000.0, ttl_s=60)
    assert e.state(1000.0) is CacheState.FRESH
    assert e.state(1060.0) is CacheState.FRESH      # boundary is inclusive
    assert e.state(1060.1) is CacheState.STALE
    assert e.age_s(1090.0) == 90


# ── Policy / events / guard ──────────────────────────────────────────────

def test_policy_decision_constructors():
    assert PolicyDecision.allow().allowed is True
    d = PolicyDecision.deny(ToolOutcome.BUDGET_EXCEEDED, "over monthly cap")
    assert d.allowed is False and d.outcome is ToolOutcome.BUDGET_EXCEEDED


def test_raw_event_passes_bytes_through_untouched():
    assert RawEvent.of(": keepalive\n\n").to_sse() == ": keepalive\n\n"


def test_scheduled_style_harness_need_not_implement_turn_control():
    """The reason Harness and InteractiveHarness are separate: a cron-fired
    push has no turn to abort and no user to steer, and should not have to
    declare no-op methods that lie about what it supports."""

    class Scheduled(Harness):
        async def run(self, turn):  # pragma: no cover - not executed
            yield RawEvent.of("")

    Scheduled()  # must not raise about unimplemented abort/steer
    assert not issubclass(Scheduled, InteractiveHarness)


def test_dev_opt_in_fails_closed():
    """A stand-in that bypasses the gateway boundary must not become
    production configuration by omission."""
    for env in ({}, {"X": ""}, {"X": "0"}, {"X": "no"}, {"X": "false"}):
        with pytest.raises(RuntimeError, match="development only"):
            require_dev_opt_in("DirectToolGateway", "X", env)
    for env in ({"X": "1"}, {"X": "true"}, {"X": "YES"}, {"X": " True "}):
        require_dev_opt_in("DirectToolGateway", "X", env)
