"""Small vocabulary of protocol/outcome constants used by the agent loop.

StrEnum (3.11+) members are real `str`s: they serialise to JSON as their
value and compare equal to plain strings, so dict-shaped LLM payloads and
existing string comparisons keep working unchanged.
"""

from enum import StrEnum


class FinishReason(StrEnum):
    """OpenAI-compatible `choices[].finish_reason` values."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    ERROR = "error"


class ToolOutcome(StrEnum):
    """How a single tool run ended, for end-of-turn outcome summaries."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    INVALID_ARGUMENTS = "invalid_arguments"
