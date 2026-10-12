"""guest.context — the request the model sees (docs/GUEST.md).

    system_block(cfg) -> str                 fixed per session (no {current_time})
    system_hash(cfg) -> str                  hash of the system text + tool schemas
    tool_schemas(cfg) -> list[dict]          OpenAI function schemas, cfg.tools order
    preamble_text(clock_at_ms, attachments=None, new_memory=()) -> str
                                             the per-turn user-side block
    build_request(state, cfg, clock_at_ms, *, attachments=None, preambles=None,
                  with_tools=True, new_memory=None) -> Request
    build_compaction_request(state, cfg, *, preambles=None) -> Request
                                             the compaction turn's request
    estimate_tokens(request) -> int          ceil(request.chars / 4), the raw estimate

The request is a pure function of (state, cfg, clock_at_ms, the attachments the
host delivered with the inputs, and the preamble texts of earlier turns), so a
replay can rebuild it from the log and compare `request_hash` (REPLAY.md
invariant 1).

Layout of `Request.messages`:

    [system] + rendered committed prefix + rendered staged suffix

The committed prefix is rendered byte-for-byte the same on every turn (the
prefix-cache invariant, REPLAY.md invariant 2): a user message committed with
preamble hash H is rendered with the preamble text registered for H; the
`preambles` dict is that registry (the coordinator owns it across turns).

The per-turn preamble (time at minute precision from `clock_at_ms`, the
recalled memory bodies attached to the input, and one `[new memory]` line per
slug in `new_memory`) is prepended to the FIRST staged user message, the one
the fold marks with the turn's `preamble_hash`. `new_memory` defaults to the
session memories written so far (state.session_memory, which the fold keeps,
compaction summaries included); the coordinator passes those merged with the
user-scope slugs the task wrote (it keeps them from its own `memory` events,
since the fold only counts user-scope writes). A turn that
commits no user message (a tool-result round) carries a preamble_hash but
inserts no preamble: the time the model sees is the last one it was told.

Tool messages the model expects but the log never produced (a call denied by
the allowlist is recorded as guard_decision only, never as tool_call) are
filled with an error tool message right after their assistant message, so the
conversation stays well-formed. Open calls are the fold's business
(placeholders in async mode) and are left alone.

Compaction. The fold rebuilds `state.committed` at a `compaction` event
(protocol.fold._compaction: items before the range, then the summary turn's
assistant message at origin range[0], then items after the range), so
`_prefix_items` renders `state.committed` as it is and the request after a
compaction starts with the summary assistant message, followed by whatever
was staged after the range. `build_compaction_request` builds the compaction
turn's own request: the system block, the committed prefix rendered exactly as
a regular turn renders it, and one user message with COMPACTION_INSTRUCTION;
no tools, no preamble (its `preamble_hash` is the hash of the instruction, so
the turn_start still records what the model was asked). Staged items are not
part of it: they stay staged and follow the summary in the next regular turn.

Token estimate: `Request.chars` is the length of the canonical JSON
text of what the model is sent ({messages, tools, params}); the same text
gives `request_hash` (sha256 of it), so it is serialised once per request.
`estimate_tokens(request)` = ceil(chars / 4), the raw estimate; the
coordinator scales it by its usage correction. Reported in
compaction.tokens_before/after; never a fold input.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from protocol.canonical import canonical_json, hash_value, sha256_hex
from protocol.fold import PLACEHOLDER

try:  # the sibling package; TOOL_DEFINITIONS may still be a stub ({}).
    from guest.tools import TOOL_DEFINITIONS
except Exception:  # pragma: no cover - import-time safety only
    TOOL_DEFINITIONS = {}

MISSING_TOOL_MESSAGE = "error: this tool call was not run (tool not offered in this session)"
CURRENT_TIME_TOKEN = "{current_time}"
CHARS_PER_TOKEN = 4
# sampling params never sent to the model as-is: `stream` is the transport
# mode the coordinator passes to the provider (config.stream_enabled)
NON_SAMPLING_PARAMS = ("stream",)
# async mode (cfg.async_tools): the fixed system-block section, and the shape of
# a result that arrives after the model saw its call's PLACEHOLDER
ASYNC_TOOLS_NOTE = (
    "## Tool calls\n"
    "Tool calls run asynchronously. A call that is still running when you are "
    f"woken shows the result {PLACEHOLDER}. Its real result arrives later as a message "
    "that starts with \"[late tool result] call_id: <id>\" naming the same call. "
    "While calls are pending, continue with work that does not depend on them, or "
    "end your turn without tool calls to wait for them.")
LATE_RESULT_HEADER = "[late tool result] call_id: {call_id}, tool: {name}"
COMPACTION_INSTRUCTION = (
    "[compaction]\n"
    "The conversation above is being compacted. Write a summary that will replace "
    "it as the only context for the rest of this session. Cover, under these "
    "headings: goal (what the user wants, their most recent request verbatim); "
    "progress (what has been done, with the exact file paths, commands and "
    "results that matter); decisions (choices made and why); next steps (what "
    "remains, in order); facts (names, values, identifiers and user "
    "preferences learned). Be concrete and complete; omit pleasantries. Do not "
    "call tools.")


@dataclass
class Request:
    messages: list
    tools: list
    params: dict
    system_hash: str
    request_hash: str
    preamble_hash: str
    chars: int = 0               # len of the canonical text request_hash is the sha256 of
    prefix_len: int = 0          # messages[:prefix_len] is the committed part (system incl.)
    preamble: str = ""           # the preamble text of this turn
    preamble_input_id: str | None = None   # the input it was prepended to, if any

    def prefix_messages(self) -> list:
        return self.messages[: self.prefix_len]

    def prefix_bytes(self) -> bytes:
        """The serialized committed part: identical between consecutive turns."""
        return json.dumps(self.prefix_messages(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- system block
def _render_block(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = []
        for key in value:            # insertion order from hello; the host fixes it
            text = _render_block(value[key])
            if text:
                parts.append(f"### {key}\n{text}")
        return "\n\n".join(parts)
    if isinstance(value, (list, tuple)):
        return "\n".join(_render_block(v) for v in value if _render_block(v))
    return str(value)


def system_block(cfg) -> str:
    """The tenant base prompt (without {current_time}), the profile block and
    the memory indexes, from cfg.system_block = {base_prompt, profile,
    memory_index}. Fixed for the session."""
    block = cfg.system_block or {}
    if isinstance(block, str):
        base, profile, index = block, "", ""
    else:
        base = _render_block(block.get("base_prompt"))
        profile = _render_block(block.get("profile"))
        index = _render_block(block.get("memory_index"))
    base = base.replace(CURRENT_TIME_TOKEN, "").strip()
    sections = [base]
    if cfg.async_tools:
        sections.append(ASYNC_TOOLS_NOTE)
    if profile:
        sections.append("## Profile\n" + profile)
    if index:
        sections.append("## Memory index\n" + index)
    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------- tools
def _as_function_schema(name: str, schema) -> dict:
    """An OpenAI `{"type": "function", "function": {...}}` entry from either
    shape (already wrapped, or the bare function dict)."""
    if isinstance(schema, dict) and schema.get("type") == "function" and "function" in schema:
        fn = dict(schema["function"])
        fn.setdefault("name", name)
        return {"type": "function", "function": fn}
    if isinstance(schema, dict) and schema:
        fn = dict(schema)
        fn.setdefault("name", name)
        fn.setdefault("parameters", {"type": "object", "properties": {}})
        return {"type": "function", "function": fn}
    return {"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}}


def tool_schemas(cfg) -> list:
    """OpenAI function schemas for cfg.tools, in cfg.tools order: guest
    built-ins from guest.tools.TOOL_DEFINITIONS; other sides from the entry's
    "schema" key or from `cfg.tool_schemas` ({name: schema}, since
    allowlist.effective_tools keeps only name/side/pin); a minimal schema
    with the name otherwise."""
    out = []
    for entry in cfg.tools:
        name = entry["name"]
        schema = entry.get("schema")
        if schema is None:
            schema = cfg.tool_schemas.get(name)
        if schema is None and entry.get("side") == "guest":
            schema = TOOL_DEFINITIONS.get(name)
        out.append(_as_function_schema(name, schema))
    return out


def _system_hash(system: str, schemas: list) -> str:
    return hash_value({"system": system, "tools": schemas})


def system_hash(cfg) -> str:
    """sha256 over the system text and the tool schemas: the fixed prefix."""
    return _system_hash(system_block(cfg), tool_schemas(cfg))


# ---------------------------------------------------------------- preamble
def format_time(clock_at_ms: int) -> str:
    """Minute precision, UTC, from a clock{turn_clock}.at value."""
    dt = datetime.fromtimestamp(clock_at_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _memory_lines(item) -> list[str]:
    """Render one recalled memory from an input's attachments. Accepts a str
    or a dict with a title-ish key and a body-ish key."""
    if isinstance(item, str):
        return ["[recalled memory]", item.strip()]
    if isinstance(item, dict):
        title = item.get("slug") or item.get("title") or item.get("name") or ""
        body = item.get("body") or item.get("text") or item.get("content") or ""
        head = f"[recalled memory: {title}]" if title else "[recalled memory]"
        return [head, str(body).strip()]
    return ["[recalled memory]", str(item)]


def preamble_text(clock_at_ms: int, attachments=None, new_memory=()) -> str:
    """The per-turn block: time, recalled memories (the input's attachments)
    and a `[new memory]` line per slug in `new_memory` (each once, sorted)."""
    lines = ["[context]", f"time: {format_time(clock_at_ms)}"]
    for item in attachments or []:
        lines.extend(_memory_lines(item))
    for slug in sorted({str(s) for s in new_memory or ()}):
        lines.append(f"[new memory] {slug}")
    return "\n".join(lines)


def _with_preamble(preamble: str, text: str) -> str:
    return preamble + "\n\n" + text if preamble else text


# ---------------------------------------------------------------- rendering
def _prefix_items(state) -> list:
    """The committed items to render. Compaction seam: the fold already
    rebuilds `state.committed` after a compaction event, so this stays the
    whole list; a window/reserve policy would select here."""
    return list(state.committed)


def _is_late(item: dict, state) -> bool:
    """A tool item that is the result of a call whose placeholder was
    committed: the fold stages it at its own seq, never at the call's."""
    message = item["message"]
    if message.get("role") != "tool":
        return False
    cid = message.get("tool_call_id")
    call = state.open_calls.get(cid)
    return (call is not None and cid in state.placeholders
            and item["origin"] != call["seq"])


def _late_message(message: dict, state) -> dict:
    """A late result as a user message naming the call (a second tool message
    for an id the model already saw answered by the placeholder is not
    well-formed chat, and may sit after other turns or after a compaction)."""
    cid = message.get("tool_call_id")
    name = state.open_calls[cid]["name"]
    header = LATE_RESULT_HEADER.format(call_id=cid, name=name)
    return {"role": "user", "content": header + "\n" + (message.get("content") or "")}


def _render(item: dict, state, preambles: dict) -> dict:
    if _is_late(item, state):
        return _late_message(item["message"], state)
    return _render_item(item["message"], preambles)


def _render_item(message: dict, preambles: dict) -> dict:
    role = message.get("role")
    if role == "user":
        text = message.get("content") or ""
        marker = message.get("preamble")
        pre = preambles.get(marker, "") if marker is not None else ""
        return {"role": "user", "content": _with_preamble(pre, text)}
    if role == "tool":
        return {"role": "tool", "tool_call_id": message.get("tool_call_id"),
                "content": message.get("content") or ""}
    return dict(message)   # assistant: verbatim


def _fill_missing_tool_messages(messages: list, known_call_ids: set, present: set) -> list:
    """After every assistant message with tool_calls, insert an error tool
    message for each call id that never became a tool_call event (so it can
    have no tool_result) and has no tool message anywhere (`present`)."""
    out = []
    for m in messages:
        out.append(m)
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            cid = tc.get("id") if isinstance(tc, dict) else None
            if cid and cid not in present and cid not in known_call_ids:
                out.append({"role": "tool", "tool_call_id": cid, "content": MISSING_TOOL_MESSAGE})
                present.add(cid)
    return out


def _params(cfg) -> dict:
    params = dict(cfg.params)
    for key in NON_SAMPLING_PARAMS:
        params.pop(key, None)
    if "max_tokens" not in params and cfg.limits.max_tokens is not None:
        params["max_tokens"] = cfg.limits.max_tokens
    return params


def _assemble(state, cfg, preambles: dict, tail: list, *, with_tools: bool, preamble: str,
              preamble_hash: str, preamble_input_id: str | None) -> Request:
    """[system] + the rendered committed prefix + `tail` (already rendered),
    with the filler tool messages, as a Request. The system text and the tool
    schemas are computed once and give system_hash too; the canonical text of
    {messages, tools, params} gives both `chars` and `request_hash`."""
    system = system_block(cfg)
    schemas = tool_schemas(cfg)
    known_calls = set(state.open_calls)
    prefix = [_render(it, state, preambles) for it in _prefix_items(state)]
    present = {m.get("tool_call_id") for m in prefix + tail if m.get("role") == "tool"}
    prefix = _fill_missing_tool_messages(prefix, known_calls, present)
    tail = _fill_missing_tool_messages(tail, known_calls, present)
    messages = [{"role": "system", "content": system}] + prefix + tail
    tools = schemas if with_tools else []
    params = _params(cfg)
    text = canonical_json({"messages": messages, "tools": tools, "params": params})
    return Request(
        messages=messages,
        tools=tools,
        params=params,
        system_hash=_system_hash(system, schemas),
        request_hash=sha256_hex(text),
        preamble_hash=preamble_hash,
        chars=len(text),
        prefix_len=1 + len(prefix),
        preamble=preamble,
        preamble_input_id=preamble_input_id,
    )


def placeholder_items(state) -> list:
    """The placeholders the fold will commit at the next turn_start
    (protocol.fold._turn_start): one PLACEHOLDER tool message per call that
    has no result yet and was not shown as a placeholder before, at the
    call's position (origin = the tool_call's seq). The fold gives them the
    turn_start's seq; any seq above every staged one sorts the same. Empty in
    sync mode (a turn never starts with a call open)."""
    after = state.last_seq + 1
    return [{"seq": after, "origin": call["seq"],
             "message": {"role": "tool", "tool_call_id": cid, "content": PLACEHOLDER}}
            for cid, call in state.open_calls.items()
            if "result" not in call and cid not in state.placeholders]


def _turn_items(state) -> list:
    """What a turn started now commits: the staged suffix and the new
    placeholders, in the fold's (origin, seq) order."""
    extra = placeholder_items(state)
    if not extra:
        return list(state.staged_suffix)
    return sorted([*state.staged_suffix, *extra], key=lambda it: (it["origin"], it["seq"]))


def build_request(state, cfg, clock_at_ms: int, *, attachments=None, preambles=None,
                  with_tools: bool = True, new_memory=None) -> Request:
    """The request for a turn started now: everything staged is what the turn
    will see (includes_through_seq = state.last_seq).

    attachments: {input_id: [recalled memory, ...]} as delivered by the host
                 with task/follow_up/steer control messages.
    preambles:   {preamble_hash: preamble text}; the registry of earlier turns'
                 preambles, updated in place with this turn's.
    with_tools:  False for the wrap-up turn after max_tools (no tools offered).
    new_memory:  the slugs listed as `[new memory]` lines; None = the session
                 memories the fold holds (state.session_memory).
    """
    attachments = attachments or {}
    preambles = preambles if preambles is not None else {}
    if new_memory is None:
        new_memory = state.session_memory

    # the staged suffix: the first user message gets this turn's preamble
    first_input = None
    for it in _turn_items(state):
        if it["message"].get("role") == "user":
            first_input = it["message"].get("input_id")
            break
    preamble = preamble_text(clock_at_ms, attachments.get(first_input), new_memory)
    p_hash = hash_value(preamble)
    preambles[p_hash] = preamble

    suffix = []
    seen_user = False
    for it in _turn_items(state):
        msg = it["message"]
        if msg.get("role") == "user" and not seen_user:
            seen_user = True
            suffix.append({"role": "user",
                           "content": _with_preamble(preamble, msg.get("content") or "")})
        else:
            suffix.append(_render(it, state, preambles))
    return _assemble(state, cfg, preambles, suffix, with_tools=with_tools, preamble=preamble,
                     preamble_hash=p_hash, preamble_input_id=first_input if seen_user else None)


# ---------------------------------------------------------------- compaction
def build_compaction_request(state, cfg, *, preambles=None) -> Request:
    """The compaction turn's request: the system block, the committed prefix
    rendered as a regular turn renders it (the same preamble registry, the
    same filler tool messages), and one user message with
    COMPACTION_INSTRUCTION. No tools; the staged suffix is not included (it
    stays staged and follows the summary in the next regular turn). The
    turn's preamble_hash is hash_value(COMPACTION_INSTRUCTION)."""
    preambles = preambles if preambles is not None else {}
    instruction = {"role": "user", "content": COMPACTION_INSTRUCTION}
    return _assemble(state, cfg, preambles, [instruction], with_tools=False,
                     preamble=COMPACTION_INSTRUCTION,
                     preamble_hash=hash_value(COMPACTION_INSTRUCTION), preamble_input_id=None)


def estimate_tokens(request) -> int:
    """The raw token estimate: ceil(request.chars / 4). The coordinator scales
    it by its usage correction."""
    return math.ceil(request.chars / CHARS_PER_TOKEN)
