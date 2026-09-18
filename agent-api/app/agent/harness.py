"""Harness implementations.

PA-5. Read the note at the top of sb_contracts.interfaces before adding a
second one: an ABC earns its place when two real implementations share a
method, not before.

Why ChatHarness implements `Harness` and not `InteractiveHarness`: `abort()`
and `steer()` do not exist yet -- they are Phase 6 work. Declaring them here as
no-ops would make the interface claim a capability the system does not have,
which is precisely the flaw that got `abort`/`steer` split off `Harness` in the
first place. When those land, change the base class on this line and implement
them for real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sb_contracts.interfaces import Harness
from sb_contracts.models import HarnessEvent, RawEvent, TurnRequest

from app.agent.loop import run_agent_loop


class ChatHarness(Harness):
    """A typed entry point onto the existing agent loop.

    This is an adapter, deliberately: `run_agent_loop` is the tested core of the
    system and was not rewritten to fit an interface invented after it. It
    yields rendered SSE frames, so they arrive here as RawEvent. The seam
    disappears when the loop itself starts yielding HarnessEvent -- worth doing
    when there is a second consumer that wants the objects rather than the
    frames, and not before.
    """

    def __init__(self, registry: object, llm: object, *,
                 system_prompt: str | None = None,
                 allowed_skills: set[str] | None = None,
                 trace: object = None) -> None:
        self._registry = registry
        self._llm = llm
        self._system_prompt = system_prompt
        self._allowed_skills = allowed_skills
        self._trace = trace

    async def run(self, turn: TurnRequest) -> AsyncIterator[HarnessEvent]:
        """One turn. History is owned by the caller's session store, not here --
        the harness is stateless so that two devices can share one conversation.
        """
        history: list[dict] = []
        kwargs = {
            "system_prompt": self._system_prompt,
            "max_tools": turn.max_tool_rounds,
            "max_tokens": turn.max_tokens,
            "allowed_skills": self._allowed_skills,
        }
        if self._trace is not None:
            kwargs["trace"] = self._trace
        async for frame in run_agent_loop(turn.text, history, self._registry,
                                          self._llm, **kwargs):
            yield RawEvent.of(frame)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChatHarness allowed={self._allowed_skills}>"
