"""Wire types and internal value objects.

The split is deliberate:
  pydantic BaseModel  -- anything crossing a process boundary or parsed from
                         config. It validates, and the boundary is where
                         untrusted input arrives.
  frozen dataclass    -- value objects passed inside one process. No validation
                         cost in the per-token path, and frozen means a tool
                         result cannot be mutated between the sanitiser and the
                         transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from sb_contracts.enums import (
    CacheState,
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

# ── Tools ────────────────────────────────────────────────────────────────


class ToolDescriptor(BaseModel):
    """One tool as offered to the model.

    `parameters` stays a raw JSON Schema dict: it is handed to the provider
    verbatim and validated against, not modelled. Nested schemas are flattened
    at the gateway before they reach the model -- Qwen3-14B handles flat
    argument schemas markedly better -- so this type does not try to express
    nesting.
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    side: ExecutionSide = ExecutionSide.SERVER
    mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    band: ToolBand = ToolBand.AVAILABLE
    requires_confirmation: bool = False
    timeout_s: int = 60

    def to_openai(self) -> dict[str, Any]:
        """The shape vLLM's `tools` parameter expects."""
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }}

    @classmethod
    def from_openai(cls, d: dict[str, Any], **overrides: Any) -> ToolDescriptor:
        fn = d.get("function", d)
        return cls(
            name=fn["name"],
            description=fn.get("description", ""),
            parameters=fn.get("parameters", {}) or {},
            **overrides,
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One requested tool invocation, arguments already parsed and validated."""

    id: str
    name: str
    arguments: dict[str, Any]
    ticket_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of one tool run.

    `content` is always a string because that is what goes back to the model in
    a `role: "tool"` message. `outcome` is what code should branch on -- never
    `content.startswith("Error")`, which cannot tell a timeout from a tool that
    legitimately returned prose beginning "Error".
    """

    call_id: str
    outcome: ToolOutcome
    content: str
    duration_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is ToolOutcome.OK

    @property
    def bytes(self) -> int:
        return len(self.content.encode("utf-8"))


# ── Turns ────────────────────────────────────────────────────────────────


class TurnRequest(BaseModel):
    """One turn's inputs. Replaces the loose kwargs on run_agent_loop()."""

    session_id: str
    tenant: str
    user: str
    origin: TurnOrigin = TurnOrigin.TEXT
    text: str = ""
    audio_ref: str | None = None
    max_tool_rounds: int | None = None
    max_tokens: int | None = None
    thinking: ThinkingLevel = ThinkingLevel.OFF

    @property
    def is_voice(self) -> bool:
        return self.origin is TurnOrigin.VOICE


# ── Harness events ───────────────────────────────────────────────────────
#
# These replace hand-built SSE f-strings. The framing lives in ONE place, so
# the contract with IndexApp is testable rather than stringly-typed, and a
# stray field or key-order change cannot slip through a Python diff unnoticed.


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    """Base. `event` is the SSE event name; `data` is its JSON payload."""

    event: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data)}\n\n"


@dataclass(frozen=True, slots=True)
class TokenEvent(HarnessEvent):
    @classmethod
    def of(cls, text: str) -> TokenEvent:
        return cls(event="token", data={"text": text})


@dataclass(frozen=True, slots=True)
class ToolCallEvent(HarnessEvent):
    @classmethod
    def of(cls, call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallEvent:
        return cls(event="tool_call",
                   data={"id": call_id, "name": name, "arguments": arguments})


@dataclass(frozen=True, slots=True)
class AvatarEvent(HarnessEvent):
    @classmethod
    def of(cls, name: str, result: Any) -> AvatarEvent:
        return cls(event="avatar_command", data={"name": name, "result": result})


@dataclass(frozen=True, slots=True)
class ConfirmEvent(HarnessEvent):
    @classmethod
    def of(cls, call_id: str, summary: str, token: str) -> ConfirmEvent:
        return cls(event="confirm",
                   data={"id": call_id, "summary": summary, "confirmation_token": token})


@dataclass(frozen=True, slots=True)
class RawEvent(HarnessEvent):
    """An already-rendered SSE frame passed through verbatim.

    Two things legitimately need this: the `: keepalive` comment frame, which is
    not an event at all, and adapters wrapping generators that predate this
    type. It is an escape hatch -- if you find yourself reaching for it to emit
    a real event, add a proper subclass instead.
    """

    raw: str = ""

    def to_sse(self) -> str:
        return self.raw

    @classmethod
    def of(cls, raw: str) -> RawEvent:
        return cls(event="raw", raw=raw)


@dataclass(frozen=True, slots=True)
class DoneEvent(HarnessEvent):
    @classmethod
    def of(cls) -> DoneEvent:
        return cls(event="done", data={})


@dataclass(frozen=True, slots=True)
class ErrorEvent(HarnessEvent):
    @classmethod
    def of(cls, message: str) -> ErrorEvent:
        return cls(event="error", data={"message": message})


# ── LLM ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting. vLLM returns this on every response and it is
    currently discarded, so nothing can answer 'why was that turn slow' or
    'how close is this session to the context wall'."""

    input: int = 0
    output: int = 0
    total: int = 0

    @classmethod
    def from_payload(cls, d: dict[str, Any] | None) -> Usage:
        d = d or {}
        return cls(
            input=int(d.get("prompt_tokens", 0) or 0),
            output=int(d.get("completion_tokens", 0) or 0),
            total=int(d.get("total_tokens", 0) or 0),
        )


class CompletionRequest(BaseModel):
    """What the provider layer is asked for, as a role plus budget rather than
    a bag of kwargs threaded through five call sites."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    role: ModelRole = ModelRole.CHAT
    thinking: ThinkingLevel = ThinkingLevel.OFF
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """One provider response, with the dict-digging done once."""

    content: str
    finish_reason: FinishReason
    tool_calls: list[dict[str, Any]]
    usage: Usage

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CompletionResponse:
        choice = payload["choices"][0]
        msg = choice.get("message", {}) or {}
        raw = choice.get("finish_reason", "stop") or "stop"
        try:
            finish = FinishReason(raw)
        except ValueError:
            finish = FinishReason.ERROR
        return cls(
            # vLLM sends content: null -- key present, so .get(k, "") yields
            # None and None.split() kills the stream.
            content=(msg.get("content") or ""),
            finish_reason=finish,
            tool_calls=list(msg.get("tool_calls") or []),
            usage=Usage.from_payload(payload.get("usage")),
        )


# ── Gateway ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    outcome: ToolOutcome = ToolOutcome.OK
    reason: str = ""
    confirmation_token: str | None = None

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(allowed=True)

    @classmethod
    def deny(cls, outcome: ToolOutcome, reason: str) -> PolicyDecision:
        return cls(allowed=False, outcome=outcome, reason=reason)


class AuditRecord(BaseModel):
    """One gateway call. `args_hash`, never `args` -- tool arguments carry the
    user's locations, stations and contacts."""

    ticket_id: str | None = None
    tenant: str
    tool: str
    args_hash: str
    bytes: int = 0
    duration_ms: int = 0
    outcome: ToolOutcome
    at: str


# ── Cache and health ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CacheEntry:
    value: Any
    fetched_at: float
    ttl_s: int

    def state(self, now: float) -> CacheState:
        return CacheState.FRESH if (now - self.fetched_at) <= self.ttl_s else CacheState.STALE

    def age_s(self, now: float) -> int:
        return int(now - self.fetched_at)


class TierHealth(BaseModel):
    """Per-tier, because the whole point of the tier split is that inference
    can be DOWN while the system is OK."""

    tier: str
    status: TierStatus
    detail: str = ""
