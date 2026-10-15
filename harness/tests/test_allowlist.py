"""allowlist: effective tool list from hello config, sides, is_allowed, tools_errors."""
import pytest

from protocol.allowlist import (
    GUEST_BUILTINS, SIDES, effective_tools, is_allowed, tool_entry_errors, tools_errors,
)


def names(tools):
    return [t["name"] for t in tools]


def test_sides_and_builtins_are_fixed():
    assert SIDES == ("guest", "host", "device", "mcp")
    assert GUEST_BUILTINS == ("read", "write", "edit", "apply_patch", "bash",
                              "grep", "glob", "ls", "todo", "question", "skill")
    assert len(set(GUEST_BUILTINS)) == len(GUEST_BUILTINS)


def test_empty_config_gives_builtins_only():
    tools = effective_tools({})
    assert names(tools) == sorted(GUEST_BUILTINS)
    assert all(t["side"] == "guest" and t["pin"] is None for t in tools)


def test_config_tools_merged_and_sorted():
    cfg = {"tools": [
        {"name": "remember", "side": "host"},
        {"name": "avatar_control", "side": "device", "pin": None},
        {"name": "wcc_search", "side": "mcp", "pin": "ab" * 32},
    ]}
    tools = effective_tools(cfg)
    assert names(tools) == sorted(list(GUEST_BUILTINS) + ["remember", "avatar_control", "wcc_search"])
    by = {t["name"]: t for t in tools}
    assert by["remember"] == {"name": "remember", "side": "host", "pin": None}
    assert by["wcc_search"]["pin"] == "ab" * 32
    assert by["avatar_control"]["side"] == "device"


def test_entries_are_normalised_to_name_side_pin_only():
    tools = effective_tools({"tools": [{"name": "x", "side": "host", "extra": 1}]})
    assert {"name": "x", "side": "host", "pin": None} in tools
    assert all(set(t) == {"name", "side", "pin"} for t in tools)


def test_exclude_builtins_drops_them():
    tools = effective_tools({"exclude_builtins": ["bash", "apply_patch"]})
    assert "bash" not in names(tools)
    assert "apply_patch" not in names(tools)
    assert "read" in names(tools)
    assert len(tools) == len(GUEST_BUILTINS) - 2


def test_exclude_unknown_builtin_rejected():
    with pytest.raises(ValueError, match="exclude_builtins"):
        effective_tools({"exclude_builtins": ["remember"]})


def test_unknown_side_rejected():
    with pytest.raises(ValueError, match="unknown side"):
        effective_tools({"tools": [{"name": "x", "side": "cloud"}]})


def test_missing_side_rejected():
    with pytest.raises(ValueError, match="unknown side"):
        effective_tools({"tools": [{"name": "x"}]})


def test_guest_side_in_config_rejected():
    with pytest.raises(ValueError, match="guest"):
        effective_tools({"tools": [{"name": "x", "side": "guest"}]})


def test_duplicate_name_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        effective_tools({"tools": [{"name": "x", "side": "host"},
                                   {"name": "x", "side": "mcp"}]})


def test_non_guest_entry_naming_builtin_rejected():
    with pytest.raises(ValueError, match="guest built-in"):
        effective_tools({"tools": [{"name": "bash", "side": "host"}]})


def test_empty_or_non_string_name_rejected():
    with pytest.raises(ValueError, match="name"):
        effective_tools({"tools": [{"name": "", "side": "host"}]})
    with pytest.raises(ValueError, match="name"):
        effective_tools({"tools": [{"name": 3, "side": "host"}]})


def test_bad_pin_rejected():
    with pytest.raises(ValueError, match="pin"):
        effective_tools({"tools": [{"name": "x", "side": "mcp", "pin": 12}]})


def test_bad_container_shapes_rejected():
    with pytest.raises(ValueError):
        effective_tools(None)
    with pytest.raises(ValueError):
        effective_tools({"tools": {"name": "x"}})
    with pytest.raises(ValueError, match=r"config.tools\[0\]: expected dict, got str"):
        effective_tools({"tools": ["x"]})
    with pytest.raises(ValueError):
        effective_tools({"exclude_builtins": "bash"})


def test_effective_tools_is_deterministic_and_pure():
    cfg = {"tools": [{"name": "b", "side": "host"}, {"name": "a", "side": "mcp", "pin": "p"}]}
    snapshot = {"tools": [dict(t) for t in cfg["tools"]]}
    assert effective_tools(cfg) == effective_tools(cfg)
    assert cfg == snapshot  # input not mutated


def test_effective_tools_output_passes_tools_errors():
    cfg = {"tools": [{"name": "remember", "side": "host"},
                     {"name": "wcc_search", "side": "mcp", "pin": "ab" * 32}]}
    assert tools_errors(effective_tools(cfg)) == []


def test_tool_entry_errors_shapes():
    assert tool_entry_errors({"name": "x", "side": "host", "pin": None}, 0) == []
    assert tool_entry_errors({"name": "x", "side": "mcp", "pin": "p"}, 0) == []
    assert tool_entry_errors("x", 2) == ["tools[2]: expected dict, got str"]
    assert tool_entry_errors({"name": "", "side": "host", "pin": None}, 0) == [
        "tools[0].name: expected non-empty str, got ''"]
    assert tool_entry_errors({"name": "x", "side": "cloud", "pin": None}, 1) == [
        "tools[1].side: unknown side 'cloud' (expected one of ['guest', 'host', 'device', 'mcp'])"]
    assert tool_entry_errors({"name": "x", "side": "host"}, 0) == [
        "tools[0].pin: missing (use null for no pin)"]
    assert tool_entry_errors({"name": "x", "side": "host", "pin": 1}, 0) == [
        "tools[0].pin: expected str | None, got int"]
    assert len(tool_entry_errors({}, 0)) == 3                   # every problem, not the first


def test_tools_errors_adds_duplicates():
    tools = [{"name": "x", "side": "host", "pin": None},
             {"name": "x", "side": "mcp", "pin": None},
             {"name": "", "side": "host", "pin": None}]
    assert tools_errors(tools) == [
        "tools[1].name: duplicate tool name 'x'",
        "tools[2].name: expected non-empty str, got ''",
    ]
    assert tools_errors(None) == ["tools: expected list, got NoneType"]
    assert tools_errors([]) == []


def test_is_allowed_matches_name_and_side():
    tools = effective_tools({"tools": [{"name": "remember", "side": "host"}]})
    assert is_allowed(tools, "remember", "host")
    assert not is_allowed(tools, "remember", "mcp")
    assert not is_allowed(tools, "remember", "guest")
    assert is_allowed(tools, "bash", "guest")
    assert not is_allowed(tools, "bash", "host")
    assert not is_allowed(tools, "nope", "guest")


def test_is_allowed_never_raises_on_junk():
    assert not is_allowed(None, "bash", "guest")
    assert not is_allowed([None, 3, {"name": "bash"}], "bash", "guest")
    assert not is_allowed([], "bash", "guest")
