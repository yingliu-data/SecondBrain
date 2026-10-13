"""limits — the default per-session limits and the timing constants both
halves share. Stdlib only, pure data.

  DEFAULT_LIMITS        hello.config.limits defaults. The guest's Limits
                        dataclass fills from it (a key the hello leaves out
                        keeps its default); the host's limits()
                        (agent-api app/host/hello.py) starts from a copy and
                        applies only env (HARNESS_*) and tenant overrides.
  SEGMENT_EVENTS        events per log segment file (seq 1..N in 000001.jsonl,
                        ...), guest buffer and host store alike. A constant,
                        not a knob: the host finds a seq's segment and counts
                        its host log by it, so changing it under an existing
                        session directory would misread it.
  HELLO_TIMEOUT_S       how long the host waits for the guest to connect, and
                        the guest for hello after idle-wait.
  QUESTION_TIMEOUT_S    hello.config.question_timeout_s default.

DEFAULT_LIMITS keys:
  wall_clock_s           a task's wall clock when its task message names none
  max_tools, max_turns   per turn / per task
  max_tokens             per LLM call
  op_timeout_s           per side: {"guest", "host", "device", "mcp"}
  spill_threshold        chars of a tool result kept inline before it spills
  spill_quota_bytes      total spill bytes per session
  grace_s                a turn's tool calls get this long after the first result
  idle_wake_s            the guest's idle timer
  context_window_tokens  the model's context length
  reserve_tokens         headroom kept for the response (compaction at window - reserve)
  park_grace_s           running guest ops get this long after `park`, then killed
  answer_wait_s          > 0 = a question deadline parks the task
"""
from __future__ import annotations

import copy

DEFAULT_LIMITS = {
    "wall_clock_s": 600,
    "max_tools": 10,
    "max_turns": 12,
    "max_tokens": 512,
    "op_timeout_s": {"guest": 120, "host": 120, "device": 60, "mcp": 300},
    "spill_threshold": 2000,
    "spill_quota_bytes": 50_000_000,
    "grace_s": 1.0,
    "idle_wake_s": 600,
    "context_window_tokens": 32768,
    "reserve_tokens": 4096,
    "park_grace_s": 30.0,
    "answer_wait_s": 0,
}

SEGMENT_EVENTS = 1000
HELLO_TIMEOUT_S = 30
QUESTION_TIMEOUT_S = 300


def default_limits() -> dict:
    """A deep copy of DEFAULT_LIMITS (safe to change)."""
    return copy.deepcopy(DEFAULT_LIMITS)
