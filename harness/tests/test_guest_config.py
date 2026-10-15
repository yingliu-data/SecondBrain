"""guest.config — GuestConfig from the hello body (docs/GUEST.md)."""
import pytest

from guest.config import (
    DEFAULT_PORTS, DEFAULT_QUESTION_TIMEOUT_S, GuestConfig, Limits, Principal, from_hello,
)
from protocol import SCHEMA_VERSION
from protocol.allowlist import GUEST_BUILTINS


def hello(**overrides) -> dict:
    body = {
        "session_id": "sess-1",
        "principal": {"user": "u", "tenant": "t"},
        "config": {
            "model": "qwen",
            "params": {"temperature": 0.2},
            "tools": [
                {"name": "calendar", "side": "device", "pin": None},
                {"name": "web_search", "side": "host"},
                {"name": "wcc.events", "side": "mcp", "pin": "sha-1"},
            ],
            "limits": {"max_turns": 3, "op_timeout_s": {"mcp": 45}},
            "hooks": {"before_tool": ["sanitize"]},
            "async_tools": False,
            "question_timeout_s": 90,
            "ui_attached": True,
        },
        "system_block": {"base_prompt": "be brief", "profile": "", "memory_index": ""},
        "resume": False,
        "at": 1_700_000_000_000,
    }
    body.update(overrides)
    return body


def test_happy_path():
    cfg = from_hello(hello())
    assert isinstance(cfg, GuestConfig)
    assert cfg.session_id == "sess-1"
    assert cfg.principal == Principal("u", "t")
    assert cfg.model == "qwen"
    assert cfg.params == {"temperature": 0.2}
    assert cfg.system_block["base_prompt"] == "be brief"
    assert not hasattr(cfg, "hooks")          # an older host's hello.config.hooks is ignored
    assert cfg.async_tools is False
    assert cfg.question_timeout_s == 90
    assert cfg.ui_attached is True
    assert cfg.resume is False
    assert cfg.at == 1_700_000_000_000


def test_tools_are_the_effective_list():
    cfg = from_hello(hello())
    names = [t["name"] for t in cfg.tools]
    assert names == sorted(names)
    assert {"name": "calendar", "side": "device", "pin": None} in cfg.tools
    assert {"name": "web_search", "side": "host", "pin": None} in cfg.tools
    assert {"name": "wcc.events", "side": "mcp", "pin": "sha-1"} in cfg.tools
    for builtin in GUEST_BUILTINS:
        assert {"name": builtin, "side": "guest", "pin": None} in cfg.tools


def test_limits_merge_over_defaults():
    cfg = from_hello(hello())
    defaults = Limits()
    assert cfg.limits.max_turns == 3
    assert cfg.limits.max_tools == defaults.max_tools
    assert cfg.limits.wall_clock_s == defaults.wall_clock_s
    assert cfg.limits.op_timeout_s == {**defaults.op_timeout_s, "mcp": 45}


def test_defaults_for_optional_fields():
    body = hello()
    body["config"] = {"model": "m"}
    del body["system_block"], body["resume"], body["at"]
    cfg = from_hello(body)
    assert cfg.params == {}
    assert cfg.limits == Limits()
    assert cfg.async_tools is False
    assert cfg.question_timeout_s == DEFAULT_QUESTION_TIMEOUT_S
    assert cfg.ui_attached is False
    assert cfg.ports == DEFAULT_PORTS
    assert cfg.system_block == {}
    assert cfg.resume is False
    assert cfg.at == 0
    assert [t["name"] for t in cfg.tools] == sorted(GUEST_BUILTINS)


def test_ports_merge_over_defaults():
    body = hello()
    body["config"]["ports"] = {"llm": 6010}
    cfg = from_hello(body)
    assert cfg.ports == {**DEFAULT_PORTS, "llm": 6010}
    body["config"]["ports"] = {"gateway": 6011}
    assert from_hello(body).ports["gateway"] == 6011
    body["config"]["ports"] = {"proxy": 5012, "gateway": 6011}   # unknown names are ignored
    assert from_hello(body).ports == {**DEFAULT_PORTS, "gateway": 6011}


def test_tool_entries_keep_schema_description_and_timeout():
    """schema/description/timeout_s of hello tool
    entries reach the model and the op timeout; session_start.tools does not
    carry them."""
    from guest.context import tool_schemas
    fn = {"type": "function", "function": {"name": "wcc-events__draft_event", "description": "Draft",
                                           "parameters": {"type": "object",
                                                          "properties": {"title": {"type": "string"}}}}}
    body = hello()
    body["config"]["tools"] = [
        {"name": "wcc-events__draft_event", "side": "mcp", "pin": "ab" * 32, "timeout_s": 2700,
         "description": "Draft", "schema": fn},
        {"name": "search_tools", "side": "mcp", "pin": "cd" * 32, "description": "Search tools"},
        {"name": "calendar", "side": "device", "pin": None},
    ]
    cfg = from_hello(body)
    assert {"name": "wcc-events__draft_event", "side": "mcp", "pin": "ab" * 32} in cfg.tools
    assert cfg.tool_schemas["wcc-events__draft_event"] == fn
    assert cfg.tool_timeouts == {"wcc-events__draft_event": 2700}
    assert cfg.op_timeout_for("wcc-events__draft_event", "mcp") == 2700.0
    assert cfg.op_timeout_for("search_tools", "mcp") == cfg.limits.op_timeout_s["mcp"]
    by_name = {s["function"]["name"]: s for s in tool_schemas(cfg)}
    assert by_name["wcc-events__draft_event"] == fn
    assert by_name["search_tools"]["function"]["description"] == "Search tools"
    assert by_name["calendar"]["function"]["parameters"] == {"type": "object", "properties": {}}
    other = hello()
    other["config"]["tools"] = [dict(body["config"]["tools"][0], timeout_s=60), *body["config"]["tools"][1:]]
    assert from_hello(other).config_hash() != cfg.config_hash()
    bad = hello()
    bad["config"]["tools"] = [{"name": "x", "side": "mcp", "pin": None, "timeout_s": "soon"}]
    with pytest.raises(ValueError, match=r"tools\[0\].timeout_s"):
        from_hello(bad)


def test_config_hash_is_stable_and_sensitive():
    a, b = from_hello(hello()), from_hello(hello())
    assert a.config_hash() == b.config_hash()
    assert len(a.config_hash()) == 64
    body = hello()
    body["config"]["model"] = "other"
    assert from_hello(body).config_hash() != a.config_hash()
    # the host clock and the system block are not part of the config hash
    body = hello(at=1, system_block={"base_prompt": "x"})
    assert from_hello(body).config_hash() == a.config_hash()


def test_missing_session_id():
    body = hello()
    del body["session_id"]
    with pytest.raises(ValueError, match="hello.session_id"):
        from_hello(body)
    with pytest.raises(ValueError, match="hello.session_id"):
        from_hello(hello(session_id=""))


def test_bad_principal():
    with pytest.raises(ValueError, match="hello.principal.tenant"):
        from_hello(hello(principal={"user": "u"}))
    with pytest.raises(ValueError, match="hello.principal: expected object"):
        from_hello(hello(principal="u/t"))


def test_unknown_tool_side():
    body = hello()
    body["config"]["tools"] = [{"name": "x", "side": "cloud", "pin": None}]
    with pytest.raises(ValueError, match="unknown side 'cloud'"):
        from_hello(body)


def test_guest_side_in_config_rejected():
    body = hello()
    body["config"]["tools"] = [{"name": "x", "side": "guest", "pin": None}]
    with pytest.raises(ValueError, match="side 'guest'"):
        from_hello(body)


def test_schema_mismatch():
    with pytest.raises(ValueError, match="schema mismatch"):
        from_hello(hello(schema=SCHEMA_VERSION + 1))
    body = hello()
    body["config"]["schema"] = 99
    with pytest.raises(ValueError, match="config.schema mismatch"):
        from_hello(body)
    assert from_hello(hello(schema=SCHEMA_VERSION)).session_id == "sess-1"


def test_missing_model_and_config():
    body = hello()
    del body["config"]["model"]
    with pytest.raises(ValueError, match="hello.config.model"):
        from_hello(body)
    body = hello()
    del body["config"]
    with pytest.raises(ValueError, match="hello.config: expected object"):
        from_hello(body)


def test_bad_limits():
    body = hello()
    body["config"]["limits"] = {"max_tools": "ten"}
    with pytest.raises(ValueError, match="limits.max_tools"):
        from_hello(body)
    body["config"]["limits"] = {"bogus": 1}
    with pytest.raises(ValueError, match="unknown limit 'bogus'"):
        from_hello(body)
    body["config"]["limits"] = {"op_timeout_s": {"cloud": 1}}
    with pytest.raises(ValueError, match="op_timeout_s: unknown side 'cloud'"):
        from_hello(body)
    body["config"]["limits"] = {"max_turns": True}
    with pytest.raises(ValueError, match="limits.max_turns"):
        from_hello(body)


def test_bad_flags_and_body():
    with pytest.raises(ValueError, match="hello.resume: expected bool"):
        from_hello(hello(resume="yes"))
    with pytest.raises(ValueError, match="hello.at"):
        from_hello(hello(at="now"))
    with pytest.raises(ValueError, match="hello.body: expected object"):
        from_hello(["not", "a", "dict"])
    body = hello()
    body["config"]["ui_attached"] = 1
    with pytest.raises(ValueError, match="config.ui_attached"):
        from_hello(body)


# ---------------------------------------------------------------- window, stream
def test_m2_limit_defaults():
    limits = Limits()
    assert (limits.context_window_tokens, limits.reserve_tokens) == (32768, 4096)
    cfg = from_hello(hello())
    assert cfg.limits.context_window_tokens == 32768 and cfg.limits.reserve_tokens == 4096


def test_m2_limits_from_hello():
    body = hello()
    body["config"]["limits"] = {"context_window_tokens": 8192, "reserve_tokens": 1024}
    cfg = from_hello(body)
    assert cfg.limits.context_window_tokens == 8192
    assert cfg.limits.reserve_tokens == 1024
    body["config"]["limits"] = {"context_window_tokens": "big"}
    with pytest.raises(ValueError, match="limits.context_window_tokens"):
        from_hello(body)
    body["config"]["limits"] = {"reserve_tokens": -1}
    with pytest.raises(ValueError, match="limits.reserve_tokens"):
        from_hello(body)


def test_token_budget_is_not_guest_config():
    # the budget is the host proxy's knob (402); it never reaches the guest,
    # so an ops change to it cannot change config_hash
    assert not hasattr(Limits(), "token_budget")
    body = hello()
    body["config"]["limits"] = {"token_budget": 5000}
    with pytest.raises(ValueError, match="unknown limit 'token_budget'"):
        from_hello(body)


def test_params_stream_default_true_and_honoured():
    from guest.config import stream_enabled
    cfg = from_hello(hello())
    assert "stream" not in cfg.params and stream_enabled(cfg) is True
    body = hello()
    body["config"]["params"] = {"temperature": 0.2, "stream": False}
    cfg = from_hello(body)
    assert cfg.params == {"temperature": 0.2, "stream": False}
    assert stream_enabled(cfg) is False
    body["config"]["params"] = {"stream": True}
    assert stream_enabled(from_hello(body)) is True
    # duck-typed: anything with (or without) a params dict
    assert stream_enabled(object()) is True
    assert stream_enabled(type("C", (), {"params": None})()) is True
    body["config"]["params"] = {"stream": "no"}
    with pytest.raises(ValueError, match="params.stream"):
        from_hello(body)


def test_stream_and_window_are_in_the_config_hash():
    a = from_hello(hello()).config_hash()
    body = hello()
    body["config"]["params"] = {"temperature": 0.2, "stream": False}
    assert from_hello(body).config_hash() != a
    body = hello()
    body["config"]["limits"] = {"max_turns": 3, "op_timeout_s": {"mcp": 45}, "context_window_tokens": 4096}
    assert from_hello(body).config_hash() != a
