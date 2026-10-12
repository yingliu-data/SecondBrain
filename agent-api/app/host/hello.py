"""hello — what a guest run is told at its start (docs/HOST.md: hello).

  build_hello(settings, tenant, ...)   the `hello` control body
  host_tool_entries(tenant, registry)  host/device tools of the allowlist
  merge_tool_entries(host, gateway)    + the gateway's mcp entries (named like
                                       a host tool: dropped)
  system_block(...)                    the fixed system block of the session
  attachments(...)                     recalled memory for an input's preamble
  limits(...)                          hello.config.limits
  answer_wait_for(tenant, driver)      limits.answer_wait_s of a guest run
  session_config(tenant, wall_clock_s) the tenant's SessionConfig
  strip_current_time(prompt)           the system prompt without {current_time}

Pure functions of their arguments (the tenant, the settings, the session
directory's memory): the Worker calls them where a guest is booted.
"""
from __future__ import annotations

import logging
import re

from flows.activities import SessionConfig
from protocol.allowlist import effective_tools
from protocol.clock import now_ms
from protocol.limits import default_limits
from protocol.version import HARNESS_VERSION

from app.config import HARNESS_MAX_QUEUE_WAIT_S, HARNESS_WALL_CLOCK_S
from app.host.outcomes import WORKFLOW
from app.host.settings import WorkerSettings
from app.user.context_builder import ContextBuilder

logger = logging.getLogger("host.hello")

_TIME_FRAGMENT = re.compile(r"\s*(?:[-*]\s*)?current time:\s*\{current_time\}\.?", re.IGNORECASE)


def strip_current_time(prompt: str) -> str:
    """The system block is fixed per session: remove the {current_time}
    placeholder (and a "Current time:" label with it); a line that held
    nothing else is dropped. The guest puts the time in its per-turn
    preamble."""
    out = []
    for line in prompt.splitlines():
        had = bool(line.strip())
        line = _TIME_FRAGMENT.sub("", line).replace("{current_time}", "").rstrip()
        if had and not line.strip():
            continue
        out.append(line)
    return "\n".join(out)


def session_config(tenant, wall_clock_s: float = HARNESS_WALL_CLOCK_S) -> SessionConfig:
    """The tenant's SessionConfig: the one reader of its SESSION_CONFIG_KEYS
    on both paths (the task signal's config, the workflow's answer timer,
    every task's wall clock). `wall_clock_s` is the wall clock of a tenant
    that sets none (HARNESS_WALL_CLOCK_S); max_queue_wait_s is
    HARNESS_MAX_QUEUE_WAIT_S (run_task's Temporal timeouts only)."""
    entry = tenant.workflow_entry()
    if entry.get("wall_clock_s") is None:
        entry["wall_clock_s"] = int(wall_clock_s)
    cfg = SessionConfig.from_tenant(entry)
    cfg.max_queue_wait_s = max(0, HARNESS_MAX_QUEUE_WAIT_S)
    return cfg


def async_tools_for(tenant, default: bool) -> bool:
    """hello.config.async_tools: the tenant's `async_tools`, else `default`
    (HARNESS_ASYNC_TOOLS)."""
    value = getattr(tenant, "async_tools", None)
    return default if value is None else bool(value)


def answer_wait_for(tenant, driver: str | None) -> int:
    """limits.answer_wait_s of a guest booted for a task of `driver`
    (DIRECT | WORKFLOW). The tenant's answer_wait_s when it sets one, on
    either path. Unset: the workflow path takes SessionConfig's default
    (86400; the workflow's durable timer waits for the answer), the
    direct path 0, the guest denies an unanswered question (no durable
    timer holds a direct park, and a 24 h park would hold the client's
    request). Applied where the hello is built (the boot), so per guest
    run: a guest booted by one driver keeps its value for later tasks."""
    explicit = tenant.workflow_entry().get("answer_wait_s")
    if explicit is not None:
        return int(explicit)
    return SessionConfig().answer_wait_s if driver == WORKFLOW else 0


def host_tool_entries(tenant, registry) -> list[dict]:
    """Host/device tools: the allowlist applied to the registry's skills,
    each with its OpenAI schema."""
    allowed = tenant.allowed_skill_names()
    server = registry.get_server_tool_names(allowed)
    device = registry.get_device_tool_names(allowed)
    entries: list[dict] = []
    seen: set[str] = set()
    for d in registry.get_active_tool_definitions(allowed):
        name = d["function"]["name"]
        if name in seen:
            continue
        side = "host" if name in server else "device" if name in device else None
        if side is None:
            continue
        seen.add(name)
        entries.append({"name": name, "side": side, "pin": None,
                        "schema": d, "description": d["function"].get("description", "")})
    return entries


def merge_tool_entries(host: list[dict], gateway: list[dict] | None,
                       label: str = "") -> list[dict]:
    """hello.config.tools: host/device tools, then the gateway's mcp entries
    verbatim except one named like a host tool. Guest built-ins are implicit."""
    names = {e["name"] for e in host}
    kept = [e for e in gateway or [] if e.get("name") not in names]
    if len(kept) != len(gateway or []):
        logger.warning("session %s: gateway tools named like host tools dropped", label)
    return host + kept


def system_block(tenant, session, user: str, default_prompt: str) -> dict:
    cb = ContextBuilder(user_id=user, session=session, base_prompt=tenant.system_prompt)
    user_idx = cb._read_if_nonempty(cb.user_memory.index)
    session_idx = cb._read_if_nonempty(cb.session_memory.index)
    index_parts = []
    if user_idx:
        index_parts.append("USER MEMORY INDEX:\n" + user_idx)
    if session_idx:
        index_parts.append("SESSION MEMORY INDEX:\n" + session_idx)
    return {
        "base_prompt": strip_current_time(tenant.system_prompt or default_prompt),
        "profile": cb.profile.to_prompt_block(),
        "memory_index": "\n\n".join(index_parts),
    }


def attachments(tenant, session, user: str, text: str) -> list[dict]:
    """Recalled memory bodies for the input's preamble."""
    cb = ContextBuilder(user_id=user, session=session, base_prompt=tenant.system_prompt)
    recalled = cb.user_memory.recall(text) + cb.session_memory.recall(text)
    return [{"kind": "memory", "type": r.type, "slug": r.slug, "name": r.name,
             "body": r.body.strip()} for r in recalled[:3]]


def limits(settings: WorkerSettings, tenant, *, wall_clock_s: int,
           answer_wait_s: float) -> dict:
    """hello.config.limits: protocol.limits.DEFAULT_LIMITS with the settings
    (HARNESS_*, whose defaults are DEFAULT_LIMITS too) and tenant overrides."""
    out = default_limits()
    out.update({
        # the default only: every task message carries its own
        "wall_clock_s": int(wall_clock_s),
        "max_tools": tenant.max_tools or settings.max_tools,
        "max_turns": settings.max_turns,
        "max_tokens": tenant.max_tokens or settings.llm_max_tokens,
        "spill_threshold": settings.spill_threshold,
        # the guest compacts when its estimate exceeds window - reserve
        # (the token budget is a proxy knob, not a guest limit)
        "context_window_tokens": settings.context_window_tokens,
        "reserve_tokens": settings.reserve_tokens,
        # running guest ops get this long after `park`, then killed
        "park_grace_s": settings.park_grace_s,
        # > 0 = a question deadline parks the task (either driver)
        "answer_wait_s": int(answer_wait_s),
    })
    out["op_timeout_s"].update(host=settings.host_tool_timeout_s,
                               device=settings.device_tool_timeout_s)
    return out


def build_hello(settings: WorkerSettings, tenant, *, session_id: str, tools: list[dict],
                system: dict, wall_clock_s: int, answer_wait_s: float,
                resume: bool = False) -> dict:
    params: dict = {"max_tokens": tenant.max_tokens or settings.llm_max_tokens,
                    "temperature": settings.llm_temperature,
                    "stream": settings.llm_stream}      # false = one call per turn
    if not settings.llm_enable_thinking:
        params["chat_template_kwargs"] = {"enable_thinking": False}
    config = {
        "model": settings.llm_model,
        "params": params,
        "tools": tools,
        "limits": limits(settings, tenant, wall_clock_s=wall_clock_s,
                         answer_wait_s=answer_wait_s),
        "async_tools": async_tools_for(tenant, settings.async_tools),
        "exclude_builtins": list(tenant.exclude_builtins),
        "question_timeout_s": settings.question_timeout_s,
        "ui_attached": True,
        "ports": dict(settings.ports),
        "harness_version": HARNESS_VERSION,
    }
    effective_tools(config)  # raises ValueError on a bad tool list, before anything runs
    return {
        "config": config,
        "session_id": session_id,
        "principal": {"user": tenant.user, "tenant": tenant.name},
        "system_block": system,
        "resume": resume,
        "at": now_ms(),
    }
