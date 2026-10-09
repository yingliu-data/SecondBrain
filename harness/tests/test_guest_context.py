"""guest.context — system block, prefix stability, preamble, tools, hashes."""
from __future__ import annotations

import json

import pytest

from guest import context
from guest.config import GuestConfig, Limits, Principal
from protocol.allowlist import effective_tools
from protocol.canonical import canonical_json, hash_value, sha256_hex
from protocol.fold import State, fold
from tests.logbuilder import Log

SID = "sess-ctx"


def make_cfg(extra_tools=(), system_block=None, params=None, **kw) -> GuestConfig:
    tools = effective_tools({"tools": list(extra_tools)})
    schemas = {t["name"]: t["schema"] for t in extra_tools if "schema" in t}
    for t in tools:                      # effective_tools keeps name/side/pin only
        if t["name"] in schemas:
            t["schema"] = schemas[t["name"]]
    return GuestConfig(
        session_id=SID,
        principal=Principal(user="u", tenant="t"),
        model="fake-model",
        params=params if params is not None else {"temperature": 0},
        tools=tools,
        system_block=system_block if system_block is not None else {
            "base_prompt": "You are SecondBrain. Now: {current_time}",
            "profile": {"name": "Ying", "tz": "Europe/Zurich"},
            "memory_index": {"user": "- likes tea", "session": ""},
        },
        limits=Limits(max_tokens=256),
        hooks={},
        async_tools=False,
        question_timeout_s=60,
        ui_attached=True,
        ports={"control": 5000, "bulk": 5001, "llm": 5010},
        resume=False,
        at=1_790_000_000_000,
        **kw,
    )


def state_with_task(text="hi", input_id="in-1") -> State:
    log = Log(SID)
    log.start()
    log.input(input_id, text)
    return fold(log.events)


# ---------------------------------------------------------------- system block
def test_system_block_strips_current_time_and_renders_sections():
    cfg = make_cfg()
    text = context.system_block(cfg)
    assert "{current_time}" not in text
    assert text.startswith("You are SecondBrain. Now:")
    assert "## Profile" in text and "### name\nYing" in text
    assert "## Memory index" in text and "- likes tea" in text
    assert "### session" not in text          # empty sections are dropped


def test_system_block_is_fixed_for_the_session():
    cfg = make_cfg()
    assert context.system_block(cfg) == context.system_block(cfg)
    assert context.system_hash(cfg) == context.system_hash(cfg)
    other = make_cfg(system_block={"base_prompt": "Other"})
    assert context.system_hash(cfg) != context.system_hash(other)


def test_system_block_accepts_a_plain_string():
    cfg = make_cfg(system_block="Just a prompt {current_time}")
    assert context.system_block(cfg) == "Just a prompt"


# ---------------------------------------------------------------- tools
def test_tool_schemas_cover_every_cfg_tool_in_order():
    cfg = make_cfg([{"name": "calendar", "side": "device", "pin": None,
                     "schema": {"name": "calendar", "description": "Read the calendar",
                                "parameters": {"type": "object", "properties": {"day": {"type": "string"}}}}},
                    {"name": "web_search", "side": "host", "pin": None}])
    schemas = context.tool_schemas(cfg)
    names = [s["function"]["name"] for s in schemas]
    assert names == [t["name"] for t in cfg.tools]
    by_name = {s["function"]["name"]: s for s in schemas}
    assert by_name["calendar"]["function"]["description"] == "Read the calendar"
    assert by_name["web_search"]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert all(s["type"] == "function" for s in schemas)


def test_tool_schema_accepts_already_wrapped_schema():
    wrapped = {"type": "function", "function": {"name": "x", "description": "d", "parameters": {"type": "object"}}}
    assert context._as_function_schema("x", wrapped) == wrapped


# ---------------------------------------------------------------- preamble
def test_preamble_time_is_minute_precision_utc():
    pre = context.preamble_text(1_790_000_000_000)
    assert pre.splitlines()[0] == "[context]"
    assert "time: 2026-09-21 14:13 UTC" in pre
    # seconds do not matter
    assert context.preamble_text(1_790_000_000_000 + 30_000) == pre


def test_preamble_lists_attachments_and_new_memories():
    pre = context.preamble_text(1_790_000_000_000,
                                [{"slug": "tea", "body": "Likes oolong."}, "plain note"], ["meetup"])
    assert "[recalled memory: tea]\nLikes oolong." in pre
    assert "[recalled memory]\nplain note" in pre
    assert "[new memory] meetup" in pre


# ---------------------------------------------------------------- build_request
def test_build_request_shape_for_the_first_turn():
    cfg = make_cfg()
    state = state_with_task("What is 2 + 2?")
    req = context.build_request(state, cfg, 1_790_000_000_000,
                                attachments={"in-1": [{"slug": "math", "body": "2+2=4"}]})
    assert req.messages[0] == {"role": "system", "content": context.system_block(cfg)}
    assert len(req.messages) == 2
    user = req.messages[1]
    assert user["role"] == "user"
    assert user["content"].endswith("\n\nWhat is 2 + 2?")
    assert user["content"].startswith("[context]\ntime: ")
    assert "[recalled memory: math]\n2+2=4" in user["content"]
    assert set(user) == {"role", "content"}        # no fold-internal keys on the wire
    assert req.preamble_hash == hash_value(req.preamble)
    text = canonical_json({"messages": req.messages, "tools": req.tools, "params": req.params})
    assert req.request_hash == sha256_hex(text) and req.chars == len(text)
    assert req.system_hash == context.system_hash(cfg)
    assert req.params == {"temperature": 0, "max_tokens": 256}
    assert req.prefix_len == 1
    assert req.preamble_input_id == "in-1"
    assert [t["function"]["name"] for t in req.tools] == [t["name"] for t in cfg.tools]


def test_build_request_without_tools_for_the_wrap_up_turn():
    cfg = make_cfg()
    req = context.build_request(state_with_task(), cfg, 1, with_tools=False)
    assert req.tools == []


def test_prefix_bytes_identical_between_consecutive_turns():
    """REPLAY.md invariant 2: the serialized prefix part of turn N+1
    starts with the whole request of turn N (system + every message it saw)."""
    cfg = make_cfg([{"name": "calendar", "side": "device", "pin": None}])
    preambles: dict = {}
    log = Log(SID, tools=cfg.tools)
    log.start()
    log.input("in-1", "What is on my calendar?")
    log.clock(1_790_000_000_000)
    state = fold(log.events)
    req1 = context.build_request(state, cfg, 1_790_000_000_000, preambles=preambles)

    # the turn commits what it saw, the model answers with a call, the result lands
    log.turn("t1", preamble=req1.preamble_hash, request_hash=req1.request_hash)
    log.response("t1", calls=[("call_1", "calendar", {"day": "today"})])
    log.call("t1", "call_1", "calendar", "device", {"day": "today"})
    log.result("call_1", "[]")
    log.clock(1_790_000_600_000)             # ten minutes later: a different preamble time
    state = fold(log.events)
    req2 = context.build_request(state, cfg, 1_790_000_600_000, preambles=preambles)

    n = len(req1.messages)
    assert req2.messages[:n] == req1.messages
    assert json.dumps(req2.messages[:n], sort_keys=True) == json.dumps(req1.messages, sort_keys=True)
    assert req2.prefix_bytes().startswith(req1.prefix_bytes()[:-1])   # same list, longer
    assert req2.prefix_len == 1 + 1   # system + the user message turn 1 committed
    assert req2.messages[req2.prefix_len:] == req2.messages[2:4]   # assistant + tool are staged
    # the tool round carries no new user message: no preamble inserted, hash still recorded
    assert req2.preamble_input_id is None
    assert req2.preamble_hash != req1.preamble_hash
    assert [m["role"] for m in req2.messages] == ["system", "user", "assistant", "tool"]

    # a third turn after a follow_up: the committed prefix is still byte-identical
    log.turn("t2", preamble=req2.preamble_hash, request_hash=req2.request_hash)
    log.response("t2", content="Nothing today.")
    log.input("in-2", "And tomorrow?", kind="follow_up")
    log.clock(1_790_000_700_000)
    state = fold(log.events)
    req3 = context.build_request(state, cfg, 1_790_000_700_000, preambles=preambles)
    assert req3.messages[: len(req2.messages)] == req2.messages
    assert req3.messages[-1]["role"] == "user"
    assert req3.messages[-1]["content"].endswith("\n\nAnd tomorrow?")
    assert req3.preamble_input_id == "in-2"


def test_request_is_a_function_of_state_cfg_clock_and_registry():
    cfg = make_cfg()
    state = state_with_task()
    a = context.build_request(state, cfg, 5_000)
    b = context.build_request(state, cfg, 5_000)
    assert a.request_hash == b.request_hash and a.preamble_hash == b.preamble_hash
    c = context.build_request(state, cfg, 5_000 + 60_000)
    assert c.preamble_hash != a.preamble_hash and c.request_hash != a.request_hash


def test_denied_call_gets_a_filler_tool_message():
    """A call the allowlist denied is recorded only as guard_decision; the
    next request still has a tool message for it, so the chat stays well-formed."""
    cfg = make_cfg()
    log = Log(SID, tools=cfg.tools)
    log.start()
    log.input("in-1", "do it")
    log.turn("t1")
    log.response("t1", calls=[("call_bad", "not_a_tool", {})])
    log.add("guard_decision", {"ref": "call_bad", "decision": "deny", "reason": "allowlist",
                               "policy_id": "allowlist"})
    state = fold(log.events)
    req = context.build_request(state, cfg, 1)
    roles = [m["role"] for m in req.messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assert req.messages[-1] == {"role": "tool", "tool_call_id": "call_bad",
                                "content": context.MISSING_TOOL_MESSAGE}


def test_open_call_is_not_filled():
    cfg = make_cfg()
    log = Log(SID, tools=cfg.tools)
    log.start()
    log.input("in-1", "do it")
    log.turn("t1")
    log.response("t1", calls=[("call_1", "bash", {"cmd": "ls"})])
    log.call("t1", "call_1", "bash", "guest", {"cmd": "ls"})
    state = fold(log.events)
    req = context.build_request(state, cfg, 1)
    assert [m["role"] for m in req.messages] == ["system", "user", "assistant"]


def test_committed_user_message_renders_with_its_registered_preamble():
    cfg = make_cfg()
    preambles: dict = {}
    log = Log(SID, tools=cfg.tools)
    log.start()
    log.input("in-1", "hello")
    state = fold(log.events)
    req1 = context.build_request(state, cfg, 1_000, preambles=preambles)
    log.turn("t1", preamble=req1.preamble_hash)
    log.response("t1", content="hi")
    log.input("in-2", "more", kind="follow_up")
    state = fold(log.events)
    req2 = context.build_request(state, cfg, 2_000_000, preambles=preambles)
    assert req2.messages[1] == req1.messages[1]
    # without the registry the preamble text is unknown and the bare text is rendered
    bare = context.build_request(state, cfg, 2_000_000, preambles={})
    assert bare.messages[1]["content"] == "hello"


@pytest.mark.parametrize("ms,expected", [
    (0, "1970-01-01 00:00 UTC"),
    (1_790_000_000_000, "2026-09-21 14:13 UTC"),
])
def test_format_time(ms, expected):
    assert context.format_time(ms) == expected


# ---------------------------------------------------------------- estimate, compaction request, memory lines
def test_estimate_tokens_is_ceil_chars_over_four():
    import math
    cfg = make_cfg()
    req = context.build_request(state_with_task(), cfg, 1)
    chars = len(json.dumps({"messages": req.messages, "tools": req.tools, "params": req.params},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    assert req.chars == chars
    assert context.estimate_tokens(req) == math.ceil(chars / 4)
    comp = context.build_compaction_request(state_with_task(), cfg)
    assert comp.request_hash == sha256_hex(canonical_json(
        {"messages": comp.messages, "tools": comp.tools, "params": comp.params}))
    assert context.estimate_tokens(comp) == math.ceil(comp.chars / 4)


def test_stream_is_not_a_sampling_param():
    cfg = make_cfg(params={"temperature": 0, "stream": False})
    req = context.build_request(state_with_task(), cfg, 1)
    assert req.params == {"temperature": 0, "max_tokens": 256}
    assert context.build_compaction_request(state_with_task(), cfg).params == req.params


def test_preamble_lists_new_memories_once_and_sorted():
    pre = context.preamble_text(1_790_000_000_000, new_memory=["plan", "both", "tea", "both", "tea"])
    lines = [l for l in pre.splitlines() if l.startswith("[new memory]")]
    assert lines == ["[new memory] both", "[new memory] plan", "[new memory] tea"]
    req = context.build_request(state_with_task(), make_cfg(), 1, new_memory=["tea"])
    assert "[new memory] tea" in req.messages[1]["content"] and "[new memory] tea" in req.preamble


def test_build_request_new_memory_defaults_to_the_session_memories():
    state = state_with_task()
    state.session_memory = {"plan": "a" * 64}
    req = context.build_request(state, make_cfg(), 1)
    assert [l for l in req.preamble.splitlines() if l.startswith("[new memory]")] == ["[new memory] plan"]
    assert "[new memory]" not in context.build_request(state, make_cfg(), 1, new_memory=[]).preamble


def _two_turn_log(cfg, preambles):
    log = Log(SID, tools=cfg.tools)
    log.start()                                                     # 1
    log.input("in-1", "What is on my calendar?")                    # 2
    log.clock(1_790_000_000_000)                                    # 3
    req1 = context.build_request(fold(log.events), cfg, 1_790_000_000_000, preambles=preambles)
    log.turn("t1", preamble=req1.preamble_hash, request_hash=req1.request_hash)   # 4
    log.response("t1", calls=[("call_1", "calendar", {"day": "today"})])           # 5
    log.call("t1", "call_1", "calendar", "device", {"day": "today"})               # 6
    log.result("call_1", "[]")                                                     # 7
    log.clock(1_790_000_600_000)                                                   # 8
    req2 = context.build_request(fold(log.events), cfg, 1_790_000_600_000, preambles=preambles)
    log.turn("t2", preamble=req2.preamble_hash, request_hash=req2.request_hash)   # 9: commits 5, 7
    log.response("t2", content="Nothing today.")                                   # 10
    log.input("in-2", "And tomorrow?", kind="follow_up")                           # 11
    return log


def test_build_compaction_request_is_system_prefix_and_instruction_without_tools():
    cfg = make_cfg([{"name": "calendar", "side": "device", "pin": None}])
    preambles: dict = {}
    log = _two_turn_log(cfg, preambles)
    state = fold(log.events)
    regular = context.build_request(state, cfg, 1_790_000_700_000, preambles=dict(preambles))
    req = context.build_compaction_request(state, cfg, preambles=preambles)
    # the committed prefix, rendered exactly as the regular turn renders it
    assert req.messages[: regular.prefix_len] == regular.prefix_messages()
    assert [m["role"] for m in req.messages] == ["system", "user", "assistant", "tool", "user"]
    assert req.messages[-1] == {"role": "user", "content": context.COMPACTION_INSTRUCTION}
    assert req.tools == [] and req.prefix_len == 4
    assert req.preamble == context.COMPACTION_INSTRUCTION
    assert req.preamble_hash == hash_value(context.COMPACTION_INSTRUCTION)
    assert req.request_hash == hash_value({"messages": req.messages, "tools": [], "params": req.params})
    assert req.system_hash == context.system_hash(cfg) and req.preamble_input_id is None
    # the staged suffix (the response and the follow-up) is not in it
    assert all("Nothing today." != m.get("content") for m in req.messages)
    assert context.build_compaction_request(state, cfg, preambles=preambles).request_hash == req.request_hash


def test_request_after_compaction_starts_with_the_summary_then_the_staged_items():
    cfg = make_cfg([{"name": "calendar", "side": "device", "pin": None}])
    preambles: dict = {}
    log = _two_turn_log(cfg, preambles)
    state = fold(log.events)
    creq = context.build_compaction_request(state, cfg, preambles=preambles)
    its = state.last_turn()["includes_through_seq"]                                # 8
    log.turn("t3", type="compaction", includes_through_seq=its,
             preamble=creq.preamble_hash, request_hash=creq.request_hash)         # 12
    log.response("t3", content="SUMMARY")                                          # 13
    log.add("compaction", {"range": [2, its], "summary_turn_id": "t3",
                           "tokens_before": 500, "tokens_after": 50})              # 14
    log.add("memory", {"scope": "session", "op": "write", "slug": "compaction-12",
                       "sha256": "0" * 64, "derived_from": "t3"})                  # 15
    log.clock(1_790_000_800_000)
    state = fold(log.events)
    assert state.committed_prefix == [{"role": "assistant", "content": "SUMMARY"}]
    assert state.prefix_origins == [2]
    req = context.build_request(state, cfg, 1_790_000_800_000, preambles=preambles)
    assert [m["role"] for m in req.messages] == ["system", "assistant", "assistant", "user"]
    assert req.messages[1] == {"role": "assistant", "content": "SUMMARY"}
    assert req.messages[2] == {"role": "assistant", "content": "Nothing today."}
    assert req.messages[3]["content"].endswith("\n\nAnd tomorrow?")
    assert "[new memory] compaction-12" in req.messages[3]["content"]
    assert req.prefix_len == 2 and req.preamble_input_id == "in-2"
    # and it stays a function of the log: the next turn renders the same prefix
    log.turn("t4", preamble=req.preamble_hash, request_hash=req.request_hash)
    log.response("t4", content="Free.")
    log.clock(1_790_000_900_000)
    req2 = context.build_request(fold(log.events), cfg, 1_790_000_900_000, preambles=preambles)
    assert req2.messages[: len(req.messages)] == req.messages
    assert req2.prefix_bytes().startswith(req.prefix_bytes()[:-1])
