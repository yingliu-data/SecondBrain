"""Planner — gate + LLM call that produces a Plan artifact.

In Phase 2 the plan is ADVISORY: it's emitted to the client and injected into
the system prompt, but execution still runs through the existing single-loop.
Phase 3 introduces an orchestrator that drives execution step by step with
parallel workers.
"""
from __future__ import annotations

import json
import logging
import uuid

from app.agent.plan import Plan, Step

logger = logging.getLogger("planner")

PLANNER_SYSTEM = """You are a PLANNER. Produce a compact JSON plan for the user's request.
Output ONLY a single valid JSON object — no prose, no code fences. Schema:

{"steps": [
  {"id": "s1",
   "goal": "<imperative step description>",
   "mode": "inline" | "worker",
   "suggested_tools": ["<tool_name>", ...],
   "depends_on": ["<step_id>", ...]}
]}

Rules:
- Prefer mode="inline"; use "worker" only for independent, self-contained steps.
- Independent steps MUST have depends_on: [] so they can run in parallel later.
- Keep plans small: MAX 4 steps. Trivial or single-tool questions → 1 inline step.
- suggested_tools MUST come from the provided tool list. Use [] when no tool is
  needed (e.g. a pure synthesis or reasoning step).
- Step ids are "s1", "s2", ... in declaration order.
"""

_MULTI_INTENT_MARKERS = (" and ", " then ", ";", "\n")
_LONG_MESSAGE_CHARS = 200
_MAX_STEPS = 4


async def should_plan(message: str, tool_defs: list[dict]) -> bool:
    """Gate: decide whether the current turn warrants a planner call.

    A planner call costs one extra LLM round-trip. Skip it for trivial queries
    where a plan adds latency without adding structure.
    """
    if not tool_defs or len(tool_defs) <= 1:
        return False
    low = (message or "").lower()
    if any(m in low for m in _MULTI_INTENT_MARKERS):
        return True
    if len(message) >= _LONG_MESSAGE_CHARS:
        return True
    return False


async def make_plan(llm, message: str, tool_defs: list[dict], session) -> Plan:
    """Generate a Plan artifact via one LLM call.

    Always returns a runnable Plan — falls back to a single-step stub on any
    failure so callers can treat the return as non-nullable.
    """
    tool_catalog = [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", "")[:200],
        }
        for t in tool_defs
    ]
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {
            "role": "user",
            "content": (
                "Tools available:\n"
                + json.dumps(tool_catalog, indent=2)
                + f"\n\nUser request:\n{message}"
            ),
        },
    ]

    # tools=None — the planner emits structured JSON text, not tool calls.
    try:
        resp = await llm.chat_completion(messages, tools=None)
    except Exception as e:
        logger.warning(f"Planner LLM call failed: {e}")
        return _fallback_plan(message, session)

    raw = (resp["choices"][0]["message"].get("content") or "").strip()
    parsed = _parse_plan_json(raw)
    if parsed is None:
        logger.warning(f"Planner returned unparseable JSON: {raw[:200]!r}")
        return _fallback_plan(message, session)

    steps = _coerce_steps(parsed, tool_defs)
    if not steps:
        return _fallback_plan(message, session)

    plan_id = "plan_" + uuid.uuid4().hex[:8]
    path = session.workspace / "plans" / f"{plan_id}.json"
    plan = Plan(plan_id=plan_id, steps=steps, path=path)
    plan.save()
    logger.info(f"Plan {plan_id} with {len(steps)} steps")
    return plan


def _parse_plan_json(raw: str) -> dict | None:
    """Strip code fences and parse. Returns None on JSON errors."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
    if s.endswith("```"):
        s = s.rsplit("```", 1)[0]
    s = s.strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _coerce_steps(data: dict, tool_defs: list[dict]) -> list[Step]:
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return []
    valid_tools = {t["function"]["name"] for t in tool_defs}
    steps: list[Step] = []
    seen_ids: set[str] = set()
    for i, s in enumerate(raw_steps[:_MAX_STEPS]):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or f"s{i+1}")
        goal = str(s.get("goal") or "").strip()
        if not goal:
            continue
        mode = s.get("mode") if s.get("mode") in ("inline", "worker") else "inline"
        tools = [
            t for t in (s.get("suggested_tools") or [])
            if isinstance(t, str) and t in valid_tools
        ]
        # Only allow deps that refer to already-declared earlier steps.
        deps = [
            d for d in (s.get("depends_on") or [])
            if isinstance(d, str) and d in seen_ids and d != sid
        ]
        steps.append(
            Step(id=sid, goal=goal, mode=mode,
                 suggested_tools=tools, depends_on=deps)
        )
        seen_ids.add(sid)
    return steps


def _fallback_plan(message: str, session) -> Plan:
    plan_id = "plan_" + uuid.uuid4().hex[:8]
    step = Step(
        id="s1",
        goal=(message[:200] or "answer the user's request").strip(),
        mode="inline",
    )
    path = session.workspace / "plans" / f"{plan_id}.json"
    plan = Plan(plan_id=plan_id, steps=[step], path=path)
    plan.save()
    return plan
