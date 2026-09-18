"""Typed failures. Deliberately few: most tool-level failures are DATA, not
exceptions -- they travel back to the model as a ToolResult carrying a
ToolOutcome, so the model can retry. Raising would lose that."""


class ContractError(Exception):
    """Base for everything in this package."""


class InvalidToolArguments(ContractError):
    """Arguments failed to parse or validate. Callers turn this into a
    ToolResult with ToolOutcome.INVALID_ARGUMENTS rather than propagating it:
    an uncaught exception here aborts the SSE stream with no terminating
    event, which is strictly worse for the client than a failed tool."""


class PolicyDenied(ContractError):
    """The gateway refused the call. Carries the outcome so the caller does not
    have to re-derive why."""

    def __init__(self, reason: str, outcome: str = "denied") -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome
