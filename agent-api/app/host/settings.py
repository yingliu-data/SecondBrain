"""settings — a Worker's configuration, one frozen value (docs/HOST.md).

  WorkerSettings.from_config(**over)   the values of app.config (read when
                                       called), `over` applied
  dataclasses.replace(settings, ...)   a variant (tests, Workers)

Dependencies (the sandbox, DeviceCalls, the Admission) are not settings:
they stay Worker keyword arguments.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass(frozen=True)
class WorkerSettings:
    # the guest run
    llm_url: str = ""
    ports: dict = field(default_factory=dict)
    hello_timeout_s: float = 30.0
    heartbeat_s: float = 15.0
    wall_clock_s: float = 600.0
    session_idle_s: float | None = 1800.0
    park_timeout_s: float = 60.0
    max_resumes: int = 1
    park_grace_s: float = 30.0
    abort_wait_s: float = 30.0
    gateway_url: str = ""
    gateway_tools_timeout_s: float = 5.0
    token_budget: int = 0
    # hello.config
    llm_model: str = ""
    llm_stream: bool = True
    llm_temperature: float = 0.7
    llm_enable_thinking: bool = True
    llm_max_tokens: int = 4096
    max_tools: int = 10
    max_turns: int = 20
    spill_threshold: int = 0
    context_window_tokens: int = 32768
    reserve_tokens: int = 4096
    host_tool_timeout_s: float = 60.0
    device_tool_timeout_s: float = 60.0
    question_timeout_s: float = 300.0
    async_tools: bool = False
    system_prompt: str = ""

    @classmethod
    def from_config(cls, **over) -> "WorkerSettings":
        from app import config as c
        values = dict(
            llm_url=c.LLM_URL, ports=dict(c.HARNESS_PORTS),
            hello_timeout_s=c.HARNESS_HELLO_TIMEOUT_S, heartbeat_s=c.HARNESS_HEARTBEAT_S,
            wall_clock_s=c.HARNESS_WALL_CLOCK_S, session_idle_s=c.HARNESS_SESSION_IDLE_S,
            park_timeout_s=c.HARNESS_PARK_TIMEOUT_S, max_resumes=c.HARNESS_MAX_RESUMES,
            park_grace_s=c.HARNESS_PARK_GRACE_S, abort_wait_s=c.HARNESS_ABORT_WAIT_S,
            gateway_url=c.MCP_GATEWAY_URL,
            gateway_tools_timeout_s=c.MCP_GATEWAY_TOOLS_TIMEOUT_S,
            token_budget=c.HARNESS_TOKEN_BUDGET,
            llm_model=c.LLM_MODEL, llm_stream=c.LLM_STREAM, llm_temperature=c.LLM_TEMPERATURE,
            llm_enable_thinking=c.LLM_ENABLE_THINKING, llm_max_tokens=c.LLM_MAX_TOKENS,
            max_tools=c.MAX_TOOLS, max_turns=c.HARNESS_MAX_TURNS,
            spill_threshold=c.HARNESS_SPILL_THRESHOLD,
            context_window_tokens=c.HARNESS_CONTEXT_WINDOW_TOKENS,
            reserve_tokens=c.HARNESS_RESERVE_TOKENS,
            host_tool_timeout_s=c.HARNESS_HOST_TOOL_TIMEOUT_S,
            device_tool_timeout_s=c.TOOL_TIMEOUT,
            question_timeout_s=c.HARNESS_QUESTION_TIMEOUT_S,
            async_tools=c.HARNESS_ASYNC_TOOLS, system_prompt=c.SYSTEM_PROMPT,
        )
        unknown = set(over) - {f.name for f in fields(cls)}
        if unknown:
            raise TypeError(f"unknown WorkerSettings field(s): {sorted(unknown)}")
        values.update(over)
        return cls(**values)

    @classmethod
    def field_names(cls) -> frozenset:
        return frozenset(f.name for f in fields(cls))
