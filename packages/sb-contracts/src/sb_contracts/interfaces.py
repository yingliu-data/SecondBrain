"""The interfaces agent-api and mcp-gateway must agree on.

BEFORE ADDING AN ABC HERE, READ THIS.

This package was written after an argument against writing it (WORK_PLAN.md,
"The case against this plan", objection 6): you cannot design a good interface
from one concrete implementation and two imagined ones. The first draft proved
the point -- it put `abort()` and `steer()` on `Harness`, and a scheduled train
push has no turn to abort and no user to steer. That was an interface flaw
visible before any implementation existed.

So the rule for this module: an ABC earns its place when a SECOND real
implementation exists and the two genuinely share the method. Until then,
prefer a concrete class. The split between `Harness` and `InteractiveHarness`
below is the fix for that first mistake, not decoration -- `ScheduledHarness`
implements only the former, and is not forced into no-op methods that lie about
what it supports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from sb_contracts.models import (
    AuditRecord,
    CompletionRequest,
    CompletionResponse,
    HarnessEvent,
    PolicyDecision,
    ToolCall,
    ToolDescriptor,
    ToolResult,
    TurnRequest,
    TierHealth,
)


class MCPClient(ABC):
    """One transport to one MCP endpoint.

    Contract: NEVER raises for tool-level failures. A tool that times out, is
    refused, or returns an error comes back as a ToolResult carrying the
    outcome, because the agent loop feeds that string back to the model so it
    can retry. An exception here aborts the SSE stream with no terminating
    event, which is strictly worse for the client than a failed tool.
    """

    @abstractmethod
    async def list_tools(self) -> list[ToolDescriptor]: ...

    @abstractmethod
    async def call_tool(self, call: ToolCall, *, timeout_s: int) -> ToolResult: ...

    @abstractmethod
    async def health(self) -> TierHealth: ...

    @abstractmethod
    async def aclose(self) -> None: ...


class MCPToolServer(ABC):
    """A tool server this project owns (Darwin, weather, the wcc groups).

    Subclasses declare descriptors and execute calls; transport wiring is the
    base class's problem, so a tool server is a pure function of its inputs and
    can be tested without a server.
    """

    name: str

    @abstractmethod
    def descriptors(self) -> list[ToolDescriptor]: ...

    @abstractmethod
    async def execute(self, call: ToolCall) -> ToolResult: ...

    @abstractmethod
    async def health(self) -> TierHealth: ...


class ToolGateway(ABC):
    """What a harness depends on to reach any tool.

    The harness never holds a credential and never chooses a transport. That is
    the whole point of the boundary: "agent-api cannot reach the internet"
    should be true because of where the process sits, not because of a check
    inside the process being protected.
    """

    @abstractmethod
    async def catalog(self, tenant: str, query: str | None = None) -> list[ToolDescriptor]: ...

    @abstractmethod
    async def call(self, tenant: str, call: ToolCall) -> ToolResult: ...

    @abstractmethod
    async def read(self, tenant: str, call: ToolCall) -> ToolResult:
        """Read-only tools only -- the dashboard refresh path, which must never
        be able to trigger a mutating call however the page is crafted."""


class GatewayPolicy(ABC):
    """One stage of the gateway pipeline; stages compose in order.

    Turns the numbered responsibilities of the gateway design into units that
    can be tested and reordered independently: allowlist, schema, confirmation,
    budget, secret injection, egress, sanitisation.
    """

    name: str

    @abstractmethod
    async def check(self, tenant: str, descriptor: ToolDescriptor,
                    call: ToolCall) -> PolicyDecision: ...


class AuditSink(ABC):
    """Where gateway decisions go. Separate from GatewayPolicy because an audit
    record is written whether or not policy allowed the call -- a denial is the
    more interesting record."""

    @abstractmethod
    async def record(self, entry: AuditRecord) -> None: ...


class LLMProviderProtocol(ABC):
    """The one call the agent loop needs. Deliberately narrow: this system has
    one provider and one set of weights, so the Models -> Provider -> API
    layering a multi-provider harness needs would be ceremony here. What it DOES
    need is the role indirection, so compaction and one-sentence phrasing carry
    different budgets without threading max_tokens everywhere."""

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...


class Harness(ABC):
    """One agent runtime: a turn in, an event stream out.

    Note what is NOT here. `abort()` and `steer()` live on InteractiveHarness
    below, because a scheduled push has neither a turn to abort nor a user to
    steer, and forcing it to implement no-ops would make the interface lie.
    """

    @abstractmethod
    def run(self, turn: TurnRequest) -> AsyncIterator[HarnessEvent]: ...


class InteractiveHarness(Harness):
    """A harness driven by a person in real time (chat, voice).

    Separate from Harness so ScheduledHarness -- the implementation that proves
    the tier split, by running with the GPU down -- is not obliged to pretend it
    supports turn control.
    """

    @abstractmethod
    async def abort(self, session_id: str) -> bool:
        """Stop an in-flight turn. Returns whether anything was running."""

    @abstractmethod
    async def steer(self, session_id: str, text: str) -> bool:
        """Inject a mid-turn correction, delivered at the next round boundary."""


def require_dev_opt_in(component: str, env_var: str, environ: dict[str, str] | Any) -> None:
    """Guard for in-process stand-ins that bypass a real boundary.

    A DirectToolGateway that runs policy inside agent-api is a fine local
    convenience and a standing bypass of everything the gateway exists to
    enforce. It must not be able to become production configuration by
    omission, so it refuses to construct unless someone opted in explicitly.
    """
    if str(environ.get(env_var, "")).strip().lower() not in ("1", "true", "yes"):
        raise RuntimeError(
            f"{component} bypasses the gateway process boundary and is for local "
            f"development only. Set {env_var}=1 to use it deliberately.")
