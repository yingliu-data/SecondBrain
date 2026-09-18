"""Closed sets of values used across SecondBrain services.

All StrEnum (3.11+): members are real `str`s, so they serialise to JSON as their
value, compare equal to plain strings, and drop into existing dict-shaped
payloads without an encoder. That property is why adopting these is a
non-breaking change rather than a migration.
"""

from enum import StrEnum


class FinishReason(StrEnum):
    """OpenAI-compatible `choices[].finish_reason`.

    LENGTH is the member that matters: it was read into a variable and never
    branched on, so truncated generations fell through to the final-text path
    and produced a dead-end apology. An enum makes the missing branch visible.
    """

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    ERROR = "error"


class ToolOutcome(StrEnum):
    """How one tool run ended.

    Replaces a `result.startswith("Error")` string test -- a sentinel
    pretending to be a type, which could not distinguish a timeout from a
    refusal from a tool that legitimately returned prose beginning "Error".
    """

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    INVALID_ARGUMENTS = "invalid_arguments"
    DENIED = "denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    BUDGET_EXCEEDED = "budget_exceeded"


class ExecutionSide(StrEnum):
    """Where a tool runs.

    GATEWAY is not yet reachable: it arrives with Phase 1, when tool calls stop
    being executed in-process and start crossing a policy boundary.
    """

    SERVER = "server"
    DEVICE = "device"
    GATEWAY = "gateway"


class ExecutionMode(StrEnum):
    """Whether a tool may run concurrently with its batch siblings.

    SEQUENTIAL is the safe default for device tools (two concurrent EventKit
    prompts on one screen is worse than the latency saved) and for anything
    requiring confirmation.
    """

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class TurnOrigin(StrEnum):
    """What started a turn. Drives tool-round budget and thinking level."""

    VOICE = "voice"
    TEXT = "text"
    SCHEDULED = "scheduled"
    EVENT = "event"


class ThinkingLevel(StrEnum):
    """Qwen3 reasoning budget for one request.

    Three levels, not Pi's seven: this system has one model. OFF is
    non-negotiable for VOICE turns -- thinking pushes a turn past ten seconds
    and makes Qwen3 describe tools instead of calling them.
    """

    OFF = "off"
    LOW = "low"
    HIGH = "high"


class ModelRole(StrEnum):
    """Which budget a request is spending, not which weights it loads."""

    CHAT = "chat"
    SUMMARISE = "summarise"
    PHRASE = "phrase"


class ToolBand(StrEnum):
    """Progressive disclosure band for a tool descriptor."""

    CORE = "core"
    AVAILABLE = "available"
    LOADED = "loaded"


class EntryType(StrEnum):
    """Session transcript entry types. Not all reach the model."""

    MESSAGE = "message"
    COMPACTION = "compaction"
    BRANCH_SUMMARY = "branch_summary"
    MODEL_CHANGE = "model_change"
    BUDGET_CHANGE = "budget_change"
    CUSTOM = "custom"
    LABEL = "label"


class CompactionReason(StrEnum):
    OVERFLOW = "overflow"
    THRESHOLD = "threshold"
    MANUAL = "manual"


class TierStatus(StrEnum):
    """Per-tier health. The tier split means DOWN inference is not DOWN system."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class CacheState(StrEnum):
    """STALE is load-bearing: the degradation ladder serves stale data with a
    label rather than an error."""

    FRESH = "fresh"
    STALE = "stale"
    MISS = "miss"
