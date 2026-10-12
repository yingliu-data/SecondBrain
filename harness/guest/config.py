"""config — the guest's configuration, built from `hello` and nothing else
(the config arrives in the first vsock message; docs/GUEST.md).

  from_hello(body) -> GuestConfig

`body` is the body of the `hello` control message:

  {config, session_id, principal: {user, tenant}, system_block, resume, at}

and `config` carries `model`, `params`, `tools` (host/device/mcp entries,
turned into the effective list by protocol.allowlist.effective_tools),
`limits` (merged over the Limits defaults, protocol.limits.DEFAULT_LIMITS),
`async_tools`, `question_timeout_s`, `ui_attached`, `ports` (default
protocol.ports.PORTS; a port name this guest does not know is logged and
ignored).
Each tools entry may also carry `schema` (OpenAI function def; a bare
`description` stands in when there is none), kept as `tool_schemas`, and
`timeout_s`, kept as `tool_timeouts` (the op timeout of that tool's calls;
part of config_hash). session_start.tools stays {name, side, pin}. A `schema` key on the body or
the config must equal protocol.SCHEMA_VERSION.

Streaming and compaction: `params.stream` (bool, default True; the
coordinator reads it with `stream_enabled(cfg)`) and the limits
`context_window_tokens` and `reserve_tokens` (the compaction threshold is
window - reserve). Park: `park_grace_s`: how long running guest operations
may finish after a `park` before they are killed. Questions:
`answer_wait_s` (int >= 0, default 0): > 0 means the host waits that long for
an answer (the Temporal path), so a question deadline parks the task
(`parked{reason: question, question_id}`) instead of denying it. The token budget is the host proxy's knob
alone (it answers 402); it is not guest config, so an ops change to it does
not change config_hash.

Every problem is a ValueError whose message names the field. Pure: no I/O
(beyond a log line for an ignored port name).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, replace

from protocol import SCHEMA_VERSION
from protocol.allowlist import SIDES, effective_tools
from protocol.canonical import hash_value, is_int
from protocol.limits import DEFAULT_LIMITS, QUESTION_TIMEOUT_S, default_limits
from protocol.ports import PORTS

log = logging.getLogger("guest.config")

DEFAULT_PORTS = PORTS
DEFAULT_QUESTION_TIMEOUT_S = QUESTION_TIMEOUT_S


@dataclass(frozen=True)
class Principal:
    user: str
    tenant: str


def _default(name: str):
    return field(default_factory=lambda: default_limits()[name])


@dataclass
class Limits:
    """hello.config.limits; every default is protocol.limits.DEFAULT_LIMITS."""
    wall_clock_s: int = DEFAULT_LIMITS["wall_clock_s"]   # a task whose task message names none
    max_tools: int = DEFAULT_LIMITS["max_tools"]
    max_turns: int = DEFAULT_LIMITS["max_turns"]
    max_tokens: int = DEFAULT_LIMITS["max_tokens"]
    op_timeout_s: dict = _default("op_timeout_s")        # per side
    spill_threshold: int = DEFAULT_LIMITS["spill_threshold"]
    spill_quota_bytes: int = DEFAULT_LIMITS["spill_quota_bytes"]
    grace_s: float = DEFAULT_LIMITS["grace_s"]
    idle_wake_s: int = DEFAULT_LIMITS["idle_wake_s"]
    context_window_tokens: int = DEFAULT_LIMITS["context_window_tokens"]   # the model's context length
    reserve_tokens: int = DEFAULT_LIMITS["reserve_tokens"]                 # headroom for the response
    park_grace_s: float = DEFAULT_LIMITS["park_grace_s"]   # guest ops get this long after `park`
    answer_wait_s: int = DEFAULT_LIMITS["answer_wait_s"]   # > 0 = a question deadline parks the task


@dataclass
class GuestConfig:
    session_id: str
    principal: Principal
    model: str
    params: dict
    tools: list            # [{name, side, pin}] — the effective list (protocol.allowlist)
    system_block: dict     # {base_prompt, profile, memory_index}
    limits: Limits
    async_tools: bool
    question_timeout_s: int
    ui_attached: bool
    ports: dict            # protocol.ports.PORTS, hello overrides applied
    resume: bool
    at: int                # host clock at hello, ms
    # what hello.config.tools entries carry beyond {name, side, pin}
    tool_schemas: dict = field(default_factory=dict)    # name -> OpenAI function def
    tool_timeouts: dict = field(default_factory=dict)   # name -> timeout_s (mcp op timeout)

    def op_timeout_for(self, name: str, side: str):
        """The op timeout of one call: the entry's timeout_s, else limits.op_timeout_s[side]."""
        t = self.tool_timeouts.get(name)
        if t:
            return float(t)
        v = (self.limits.op_timeout_s or {}).get(side)
        return float(v) if v else None

    def config_hash(self) -> str:
        """sha256 of the canonical form of everything the model and the loop
        depend on: what session_start.config_hash records."""
        return hash_value({
            "model": self.model,
            "params": self.params,
            "tools": self.tools,
            "limits": _limits_dict(self.limits),
            "async_tools": self.async_tools,
            "question_timeout_s": self.question_timeout_s,
            "ui_attached": self.ui_attached,
            "tool_timeouts": self.tool_timeouts,
        })


def _limits_dict(limits: Limits) -> dict:
    return {f.name: getattr(limits, f.name) for f in fields(Limits)}


# ---------------------------------------------------------------- checks


def _dict(value, name: str, default=None) -> dict:
    if value is None and default is not None:
        return dict(default)
    if not isinstance(value, dict):
        raise ValueError(f"hello.{name}: expected object, got {type(value).__name__}")
    return value


def _str(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"hello.{name}: expected non-empty string, got {value!r}")
    return value


def _bool(value, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"hello.{name}: expected bool, got {value!r}")
    return value


def _int(value, name: str, default: int | None = None, minimum: int = 0) -> int:
    if value is None and default is not None:
        return default
    if not is_int(value, minimum):
        raise ValueError(f"hello.{name}: expected int >= {minimum}, got {value!r}")
    return value


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"hello.{name}: expected non-negative number, got {value!r}")
    return value


def _check_schema(container: dict, name: str) -> None:
    if "schema" in container and container["schema"] != SCHEMA_VERSION:
        raise ValueError(
            f"hello.{name}schema mismatch: {container['schema']!r} != {SCHEMA_VERSION}")


def stream_enabled(cfg) -> bool:
    """`params.stream`, default True (LLM_STREAM=false on the host sets it false)."""
    return bool((getattr(cfg, "params", None) or {}).get("stream", True))


def _params(raw) -> dict:
    params = _dict(raw, "config.params", {})
    if "stream" in params:
        _bool(params["stream"], "config.params.stream", True)
    return params


def _limits(raw) -> Limits:
    """Limits merged over the defaults; unknown keys and wrong types rejected."""
    raw = _dict(raw, "config.limits", {})
    defaults = Limits()
    known = {f.name for f in fields(Limits)}
    merged: dict = {}
    for key, value in raw.items():
        if key not in known:
            raise ValueError(f"hello.config.limits: unknown limit {key!r} "
                             f"(known: {sorted(known)})")
        if key == "op_timeout_s":
            timeouts = dict(defaults.op_timeout_s)
            for side, t in _dict(value, "config.limits.op_timeout_s").items():
                if side not in SIDES:
                    raise ValueError(
                        f"hello.config.limits.op_timeout_s: unknown side {side!r}")
                timeouts[side] = _number(t, f"config.limits.op_timeout_s.{side}")
            merged[key] = timeouts
        elif key in ("grace_s", "park_grace_s"):
            merged[key] = _number(value, f"config.limits.{key}")
        else:
            merged[key] = _int(value, f"config.limits.{key}")
    return replace(defaults, **merged)


def _ports(raw) -> dict:
    raw = _dict(raw, "config.ports", {})
    ports = dict(DEFAULT_PORTS)
    for name, port in raw.items():
        if name not in DEFAULT_PORTS:
            # a newer host may name ports this guest does not use: not a reason to fail the hello
            log.warning("hello.config.ports: unknown port %r ignored (known: %s)",
                        name, sorted(DEFAULT_PORTS))
            continue
        ports[name] = _int(port, f"config.ports.{name}", minimum=1)
    return ports


def _tool_extras(raw) -> tuple[dict, dict]:
    """({name: schema}, {name: timeout_s}) from hello.config.tools entries
    (effective_tools keeps only name/side/pin). A `description` without a
    `schema` becomes a schema with that description and no parameters."""
    schemas: dict = {}
    timeouts: dict = {}
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        schema = entry.get("schema")
        if schema is not None:
            schemas[name] = _dict(schema, f"config.tools[{i}].schema")
        elif isinstance(entry.get("description"), str) and entry["description"]:
            schemas[name] = {"name": name, "description": entry["description"]}
        if entry.get("timeout_s") is not None:
            timeouts[name] = _number(entry["timeout_s"], f"config.tools[{i}].timeout_s")
    return schemas, timeouts


def _principal(raw) -> Principal:
    raw = _dict(raw, "principal")
    return Principal(user=_str(raw.get("user"), "principal.user"),
                     tenant=_str(raw.get("tenant"), "principal.tenant"))


# ---------------------------------------------------------------- the builder


def from_hello(body) -> GuestConfig:
    """Build the session's configuration from a hello body. Raises ValueError
    naming the offending field for anything that does not fit."""
    body = _dict(body, "body")
    _check_schema(body, "")
    session_id = _str(body.get("session_id"), "session_id")
    principal = _principal(body.get("principal"))
    config = _dict(body.get("config"), "config")
    _check_schema(config, "config.")

    try:
        tools = effective_tools(config)
    except ValueError as exc:
        raise ValueError(f"hello.{exc}") from None
    schemas, timeouts = _tool_extras(config.get("tools"))

    return GuestConfig(
        session_id=session_id,
        principal=principal,
        model=_str(config.get("model"), "config.model"),
        params=_params(config.get("params")),
        tools=tools,
        system_block=_dict(body.get("system_block"), "system_block", {}),
        limits=_limits(config.get("limits")),
        async_tools=_bool(config.get("async_tools"), "config.async_tools", False),
        question_timeout_s=_int(config.get("question_timeout_s"), "config.question_timeout_s",
                                DEFAULT_QUESTION_TIMEOUT_S),
        ui_attached=_bool(config.get("ui_attached"), "config.ui_attached", False),
        ports=_ports(config.get("ports")),
        resume=_bool(body.get("resume"), "resume", False),
        at=_int(body.get("at"), "at", 0),
        tool_schemas=schemas,
        tool_timeouts=timeouts,
    )
