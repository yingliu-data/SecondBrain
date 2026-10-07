"""guest.context — the request the model sees (PLAN.md section 7; docs/GUEST.md).

    system_block(cfg) -> str                 fixed per session (no {current_time})
    system_hash(cfg) -> str                  hash of the system text + tool schemas
    tool_schemas(cfg) -> list[dict]          OpenAI function schemas, cfg.tools order
    preamble_text(state, clock_at_ms, attachments) -> str
                                             the per-turn user-side block
    build_request(state, cfg, clock_at_ms, *, attachments=None, preambles=None,
                  with_tools=True) -> Request

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
recalled memory bodies attached to the input, `[new memory]` lines for the
session memories written so far) is prepended to the FIRST staged user
message, the one the fold marks with the turn's `preamble_hash`. A turn that
commits no user message (a tool-result round) carries a preamble_hash but
inserts no preamble: the time the model sees is the last one it was told.

Tool messages the model expects but the log never produced (a call denied by
the allowlist is recorded as guard_decision only, never as tool_call) are
filled with an error tool message right after their assistant message, so the
conversation stays well-formed. Open calls are the fold's business
(placeholders in async mode) and are left alone.

Compaction (M2) hooks in at `_prefix_items`: it is the one place that decides
which committed items are rendered; today it is all of them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from protocol.canonical import hash_value

try:  # the sibling package; TOOL_DEFINITIONS may still be a stub ({}).
    from guest.tools import TOOL_DEFINITIONS
except Exception:  # pragma: no cover - import-time safety only
    TOOL_DEFINITIONS = {}

MISSING_TOOL_MESSAGE = "error: this tool call was not run (tool not offered in this session)"
CURRENT_TIME_TOKEN = "{current_time}"


@dataclass
class Request:
    messages: list
    tools: list
    params: dict
    system_hash: str
    request_hash: str
    preamble_hash: str
    prefix_len: int = 0          # messages[:prefix_len] is the committed part (system incl.)
    preamble: str = ""           # the preamble text of this turn
    preamble_input_id: str | None = None   # the input it was prepended to, if any
    meta: dict = field(default_factory=dict)

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
    block = getattr(cfg, "system_block", None) or {}
    if isinstance(block, str):
        base, profile, index = block, "", ""
    else:
        base = _render_block(block.get("base_prompt"))
        profile = _render_block(block.get("profile"))
        index = _render_block(block.get("memory_index"))
    base = base.replace(CURRENT_TIME_TOKEN, "").strip()
    sections = [base]
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
    extra = getattr(cfg, "tool_schemas", None) or {}
    out = []
    for entry in getattr(cfg, "tools", None) or []:
        name = entry["name"]
        schema = entry.get("schema")
        if schema is None:
            schema = extra.get(name) if isinstance(extra, dict) else None
        if schema is None and entry.get("side") == "guest":
            schema = TOOL_DEFINITIONS.get(name)
        out.append(_as_function_schema(name, schema))
    return out


def system_hash(cfg) -> str:
    """sha256 over the system text and the tool schemas: the fixed prefix."""
    return hash_value({"system": system_block(cfg), "tools": tool_schemas(cfg)})


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


def preamble_text(state, clock_at_ms: int, attachments=None) -> str:
    """The per-turn block: time, recalled memories (the input's attachments),
    `[new memory]` lines for session memories written so far (state.session_memory)."""
    lines = ["[context]", f"time: {format_time(clock_at_ms)}"]
    for item in attachments or []:
        lines.extend(_memory_lines(item))
    for slug in sorted(getattr(state, "session_memory", {}) or {}):
        lines.append(f"[new memory] {slug}")
    return "\n".join(lines)


def _with_preamble(preamble: str, text: str) -> str:
    return preamble + "\n\n" + text if preamble else text


# ---------------------------------------------------------------- rendering
def _prefix_items(state) -> list:
    """The committed items to render. Compaction seam (M2): the fold already
    rebuilds `state.committed` after a compaction event, so this stays the
    whole list; a window/reserve policy would select here."""
    return list(state.committed)


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
    params = dict(getattr(cfg, "params", None) or {})
    limits = getattr(cfg, "limits", None)
    max_tokens = getattr(limits, "max_tokens", None)
    if "max_tokens" not in params and max_tokens is not None:
        params["max_tokens"] = max_tokens
    return params


def build_request(state, cfg, clock_at_ms: int, *, attachments=None, preambles=None,
                  with_tools: bool = True) -> Request:
    """The request for a turn started now: everything staged is what the turn
    will see (includes_through_seq = state.last_seq).

    attachments: {input_id: [recalled memory, ...]} as delivered by the host
                 with task/follow_up/steer control messages.
    preambles:   {preamble_hash: preamble text}; the registry of earlier turns'
                 preambles, updated in place with this turn's.
    with_tools:  False for the wrap-up turn after max_tools (no tools offered).
    """
    attachments = attachments or {}
    preambles = preambles if preambles is not None else {}
    known_calls = set(getattr(state, "open_calls", {}) or {})

    system = {"role": "system", "content": system_block(cfg)}
    prefix = [_render_item(it["message"], preambles) for it in _prefix_items(state)]

    # the staged suffix: the first user message gets this turn's preamble
    first_input = None
    for it in state.staged_suffix:
        if it["message"].get("role") == "user":
            first_input = it["message"].get("input_id")
            break
    preamble = preamble_text(state, clock_at_ms, attachments.get(first_input))
    p_hash = hash_value(preamble)
    preambles[p_hash] = preamble

    suffix = []
    seen_user = False
    for it in state.staged_suffix:
        msg = it["message"]
        if msg.get("role") == "user" and not seen_user:
            seen_user = True
            suffix.append({"role": "user",
                           "content": _with_preamble(preamble, msg.get("content") or "")})
        else:
            suffix.append(_render_item(msg, preambles))
    present = {m.get("tool_call_id") for m in prefix + suffix if m.get("role") == "tool"}
    prefix = _fill_missing_tool_messages(prefix, known_calls, present)
    suffix = _fill_missing_tool_messages(suffix, known_calls, present)

    messages = [system] + prefix + suffix
    tools = tool_schemas(cfg) if with_tools else []
    params = _params(cfg)
    return Request(
        messages=messages,
        tools=tools,
        params=params,
        system_hash=system_hash(cfg),
        request_hash=hash_value({"messages": messages, "tools": tools, "params": params}),
        preamble_hash=p_hash,
        prefix_len=1 + len(prefix),
        preamble=preamble,
        preamble_input_id=first_input if seen_user else None,
    )
