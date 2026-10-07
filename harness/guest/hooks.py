"""guest.hooks — the before_tool / after_tool chains (PLAN.md D5, section 8).

    before_tool(state, call) -> ("allow" | "deny" | "ask", reason)
    after_tool(state, call, content) -> str | None        a rewrite, or None
    Hooks                                                 ordered chains; M2 registers
                                                          sanitize / avatar rewrites

`call` is the tool_call body ({turn_id, call_id, name, side, args, ...}).

M1 ships the pass-through defaults: the before chain is empty and answers
("allow", "placeholder_pass"); the after chain is empty and rewrites nothing.
The coordinator still records a guard_decision for every call (D5), so the
log shows the slot even while the policy is a placeholder.

Purity rule: a hook is a pure function of (state, call, content). It never
reads the clock, the environment, randomness, a file or the network, and it
never calls the LLM, a tool or the gateway. Replay re-runs nothing, so a hook
that depended on anything outside its arguments would make the log lie.
PLAN.md section 14: "a hook reading time() → purity test fails".
"""
from __future__ import annotations

PASS_REASON = "placeholder_pass"
DECISIONS = ("allow", "deny", "ask")


class Hooks:
    """Ordered chains. `before` hooks run in registration order and the first
    non-allow answer wins (a deny or an ask stops the chain); `after` hooks
    run in order, each seeing the previous rewrite, and the final text is the
    rewrite (None when no hook changed anything)."""

    def __init__(self):
        self._before: list = []
        self._after: list = []

    # registration (M2: sanitize, avatar rewrite)
    def register_before(self, fn) -> None:
        self._before.append(fn)

    def register_after(self, fn) -> None:
        self._after.append(fn)

    @property
    def before_chain(self) -> list:
        return list(self._before)

    @property
    def after_chain(self) -> list:
        return list(self._after)

    # the two entry points
    def before_tool(self, state, call: dict) -> tuple[str, str]:
        """("allow", "placeholder_pass") from an empty chain; otherwise the
        first deny/ask, or the last hook's allow reason."""
        reason_out = PASS_REASON
        for fn in self._before:
            decision, reason = fn(state, call)
            if decision not in DECISIONS:
                raise ValueError(f"before_tool hook returned {decision!r}, expected one of {DECISIONS}")
            if decision != "allow":
                return decision, str(reason)
            reason_out = str(reason)
        return "allow", reason_out

    def after_tool(self, state, call: dict, content: str):
        text = content
        changed = False
        for fn in self._after:
            out = fn(state, call, text)
            if out is not None and out != text:
                text = out
                changed = True
        return text if changed else None


DEFAULT = Hooks()


def before_tool(state, call: dict) -> tuple[str, str]:
    """Module-level pass-through: the default chain."""
    return DEFAULT.before_tool(state, call)


def after_tool(state, call: dict, content: str):
    return DEFAULT.after_tool(state, call, content)
