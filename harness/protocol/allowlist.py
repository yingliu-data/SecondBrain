"""allowlist — effective tool list and sides from hello.

Public contract (other modules import these names):
  SIDES = ("guest", "host", "device", "mcp")
  GUEST_BUILTINS: tuple of guest tool names
  effective_tools(hello_config) -> list[dict]   [{name, side, pin}] sorted by name
  is_allowed(tools, name, side) -> bool
  tool_entry_errors(entry, index) -> list[str]  shape of one {name, side, pin}
  tools_errors(tools) -> list[str]              every entry + duplicate names

The effective list is fixed at session_start: the host/device/mcp tools named
in hello's config["tools"] plus the guest built-ins (side "guest", pin None),
minus config["exclude_builtins"]. It is recorded verbatim in
session_start.tools and is the allowlist for every tool_call in the session.
`tools_errors` is the one definition of a well-formed tool list: both
effective_tools (on the hello config) and events.validate_event (on
session_start.tools) use it.

Everything here is pure: no I/O, no clock, no randomness.
"""
from __future__ import annotations

SIDES = ("guest", "host", "device", "mcp")

GUEST_BUILTINS = (
    "read", "write", "edit", "apply_patch", "bash", "grep", "glob", "ls",
    "todo", "question", "skill",
)

_CONFIG_SIDES = tuple(s for s in SIDES if s != "guest")


def tool_entry_errors(entry, index: int) -> list[str]:
    """Shape errors of one tool entry, each prefixed 'tools[index]': a dict with
    a non-empty str name, a side in SIDES and a pin key that is str | None."""
    if not isinstance(entry, dict):
        return [f"tools[{index}]: expected dict, got {type(entry).__name__}"]
    errors: list[str] = []
    name, side = entry.get("name"), entry.get("side")
    if not isinstance(name, str) or not name:
        errors.append(f"tools[{index}].name: expected non-empty str, got {name!r}")
    if side not in SIDES:
        errors.append(
            f"tools[{index}].side: unknown side {side!r} (expected one of {list(SIDES)})")
    if "pin" not in entry:
        errors.append(f"tools[{index}].pin: missing (use null for no pin)")
    elif entry["pin"] is not None and not isinstance(entry["pin"], str):
        errors.append(
            f"tools[{index}].pin: expected str | None, got {type(entry['pin']).__name__}")
    return errors


def tools_errors(tools) -> list[str]:
    """Every entry's errors plus duplicate names. [] == a well-formed list."""
    if not isinstance(tools, list):
        return [f"tools: expected list, got {type(tools).__name__}"]
    errors: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(tools):
        errors.extend(tool_entry_errors(entry, i))
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name:
            if name in seen:
                errors.append(f"tools[{i}].name: duplicate tool name {name!r}")
            seen.add(name)
    return errors


def effective_tools(hello_config) -> list[dict]:
    """Build the session's tool list from the hello config.

    config["tools"]: list of {name, side, pin?} where side is host/device/mcp
    (guest tools are built in, never configured). Missing "tools" means [].
    config["exclude_builtins"]: optional list of guest built-in names to drop.

    Raises ValueError with a clear message on: non-dict config, non-list
    tools, a "guest" side in config, a non-guest entry naming a guest
    built-in, an unknown name in exclude_builtins, and anything tools_errors
    reports (non-dict entry, empty or non-string name, unknown side, a pin
    that is not str | None, a duplicate name).
    """
    if not isinstance(hello_config, dict):
        raise ValueError(
            f"hello config must be a dict, got {type(hello_config).__name__}")
    raw = hello_config.get("tools", [])
    if not isinstance(raw, list):
        raise ValueError(f"config.tools must be a list, got {type(raw).__name__}")
    excluded_raw = hello_config.get("exclude_builtins", [])
    if not isinstance(excluded_raw, list):
        raise ValueError(
            f"config.exclude_builtins must be a list, got {type(excluded_raw).__name__}")
    excluded: set[str] = set()
    for name in excluded_raw:
        if name not in GUEST_BUILTINS:
            raise ValueError(
                f"config.exclude_builtins: {name!r} is not a guest built-in "
                f"{list(GUEST_BUILTINS)}")
        excluded.add(name)

    tools: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"config.{tool_entry_errors(entry, i)[0]}")
        name, side = entry.get("name"), entry.get("side")
        if side == "guest":
            raise ValueError(
                f"config.tools[{i}]: {name!r} has side 'guest'; guest tools are "
                f"built in and never configured")
        if name in GUEST_BUILTINS:
            raise ValueError(
                f"config.tools[{i}]: {name!r} is a guest built-in and cannot be "
                f"offered on side {side!r}")
        tools.append({"name": name, "side": side, "pin": entry.get("pin")})
    errors = tools_errors(tools)
    if errors:
        raise ValueError(f"config.{errors[0]}")

    for name in GUEST_BUILTINS:
        if name not in excluded:
            tools.append({"name": name, "side": "guest", "pin": None})

    tools.sort(key=lambda t: t["name"])
    return tools


def is_allowed(tools, name, side) -> bool:
    """True when `tools` (as effective_tools returns, or session_start.tools)
    offers a tool with exactly this name and side. Never raises."""
    if not isinstance(tools, (list, tuple)):
        return False
    for t in tools:
        if isinstance(t, dict) and t.get("name") == name and t.get("side") == side:
            return True
    return False
