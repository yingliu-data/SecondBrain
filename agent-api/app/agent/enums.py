"""Re-export shim. The definitions live in `sb_contracts.enums`.

These two enums were added here during P0, before the shared contracts package
existed, because they fix live bugs (an unhandled `finish_reason == "length"`,
and a `result.startswith("Error")` sentinel) and should not have waited on a
refactor. Phase 0.5 moved the definitions into `sb_contracts` so agent-api and
mcp-gateway share one vocabulary.

Keeping this module as a shim rather than deleting it is deliberate: two
structurally identical StrEnum classes compare equal by value but NOT by
identity, so a stale import here plus an `is` comparison in
`sb_contracts.models.ToolResult.ok` would silently report a successful tool
call as failed. A shim makes that impossible to reintroduce by accident.

Import from `sb_contracts.enums` in new code.
"""

from sb_contracts.enums import FinishReason, ToolOutcome

__all__ = ["FinishReason", "ToolOutcome"]
