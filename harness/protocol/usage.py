"""usage — one rule for an OpenAI-style `usage` dict. Stdlib only.

  normalise_usage(usage) -> {"prompt_tokens", "completion_tokens", "total_tokens"}

Counts that are not ints (absent, None, a float, a string) are 0;
`total_tokens` is the upstream's when it is a positive int, else
prompt_tokens + completion_tokens. Used by the guest provider (its running
sums), the coordinator (a task's usage delta) and the host's LLM proxy (the
`usage` host event and the token budget).
"""
from __future__ import annotations

from protocol.canonical import is_int

USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _count(value) -> int:
    return value if is_int(value) else 0


def normalise_usage(usage) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    total = _count(usage.get("total_tokens"))
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total if total > 0 else prompt + completion}
