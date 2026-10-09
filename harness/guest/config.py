"""config — the guest's configuration, built from `hello` and nothing else
(PLAN.md ground rule "config arrives in the first vsock message"; docs/GUEST.md).

  from_hello(body) -> GuestConfig

`body` is the body of the `hello` control message:

  {config, session_id, principal: {user, tenant}, system_block, resume, at}

and `config` carries `model`, `params`, `tools` (host/device/mcp entries,
turned into the effective list by protocol.allowlist.effective_tools),
`limits` (merged over the Limits defaults), `hooks`, `async_tools`,
`question_timeout_s`, `ui_attached`, `ports` (M4: `gateway`, default 5011).
M4: each tools entry may also carry `schema` (OpenAI function def; a bare
`description` stands in when there is none), kept as `tool_schemas`, and
`timeout_s`, kept as `tool_timeouts` (the op timeout of that tool's calls;
part of config_hash). session_start.tools stays {name, side, pin}. A `schema` key on the body or
the config must equal protocol.SCHEMA_VERSION.

M2 (docs/GUEST.md "M2 contracts"): `params.stream` (bool, default True; the
coordinator reads it with `stream_enabled(cfg)`) and the limits
`context_window_tokens` (32768) and `reserve_tokens` (4096: the compaction
threshold is window - reserve). M3: `park_grace_s` (30): how long running
guest operations may finish after a `park` before they are killed. The token budget is the host proxy's knob
alone (it answers 402); it is not guest config, so an ops change to it does
not change config_hash.

Every problem is a ValueError whose message names the field. Pure: no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

from protocol import SCHEMA_VERSION
from protocol.allowlist import SIDES, effective_tools
from protocol.canonical import hash_value, is_int

DEFAULT_PORTS = {"control": 5000, "bulk": 5001, "llm": 5010, "gateway": 5011}
DEFAULT_QUESTION_TIMEOUT_S = 300


@dataclass(frozen=True)
class Principal:
    user: str
    tenant: str


@dataclass
class Limits:
    wall_clock_s: int = 600
    max_tools: int = 10
    max_turns: int = 12
    max_tokens: int = 512
    op_timeout_s: dict = field(default_factory=lambda: {"guest": 120, "host": 120, "device": 60, "mcp": 300})
    spill_threshold: int = 2000
    spill_quota_bytes: int = 50_000_000
    grace_s: float = 1.0
    idle_wake_s: int = 600
    context_window_tokens: int = 32768   # M2: the model's context length
    reserve_tokens: int = 4096           # M2: headroom kept for the response
    park_grace_s: float = 30.0           # M3: guest ops get this long after `park`, then killed


@dataclass
class GuestConfig:
    session_id: str
    principal: Principal
    model: str
    params: dict
    tools: list            # [{name, side, pin}] — the effective list (protocol.allowlist)
    system_block: dict     # {base_prompt, profile, memory_index}
    limits: Limits
    hooks: dict
    async_tools: bool
    question_timeout_s: int
    ui_attached: bool
    ports: dict            # {"control": 5000, "bulk": 5001, "llm": 5010, "gateway": 5011}
    resume: bool
    at: int                # host clock at hello, ms
    # M4: what hello.config.tools entries carry beyond {name, side, pin}
    tool_schemas: dict = field(default_factory=dict)    # name -> OpenAI function def
    tool_timeouts: dict = field(default_factory=dict)   # name -> timeout_s (mcp op timeout)

    def op_timeout_for(self, name: str, side: str):
        """The op timeout of one call: the entry's timeout_s (M4-CONTRACTS
        section 4), else limits.op_timeout_s[side]."""
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
            "hooks": self.hooks,
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
    """`params.stream`, default True (D17: LLM_STREAM=false on the host sets it false)."""
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
            raise ValueError(f"hello.config.ports: unknown port {name!r} "
                             f"(known: {sorted(DEFAULT_PORTS)})")
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
        hooks=_dict(config.get("hooks"), "config.hooks", {}),
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
