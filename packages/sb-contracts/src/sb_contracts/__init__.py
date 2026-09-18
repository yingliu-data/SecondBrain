"""Shared contracts for SecondBrain services.

Two services must agree on these: `agent-api` (the orchestrator) and, from
Phase 1, `mcp-gateway` (the policy device). The transport code between them is
deliberately NOT shared -- the gateway injects credentials the agent side must
never hold -- but the vocabulary is, because it is the thing both ends parse.

Layering, deliberately:
  enums.py       closed sets of values. StrEnum, so they serialise as plain
                 strings and compare equal to them -- existing dict-shaped LLM
                 payloads and SSE frames keep working untouched.
  models.py      pydantic at process boundaries (it validates, and boundaries
                 are where untrusted input arrives); frozen dataclasses inside
                 one process (no validation cost in the per-token path, and a
                 tool result cannot be mutated between sanitiser and transcript).
  interfaces.py  the ABCs. Read the note at the top of that module before
                 adding one.
"""

from sb_contracts.enums import (
    CacheState,
    CompactionReason,
    EntryType,
    ExecutionMode,
    ExecutionSide,
    FinishReason,
    ModelRole,
    ThinkingLevel,
    TierStatus,
    ToolBand,
    ToolOutcome,
    TurnOrigin,
)

__all__ = [
    "CacheState",
    "CompactionReason",
    "EntryType",
    "ExecutionMode",
    "ExecutionSide",
    "FinishReason",
    "ModelRole",
    "ThinkingLevel",
    "TierStatus",
    "ToolBand",
    "ToolOutcome",
    "TurnOrigin",
]
