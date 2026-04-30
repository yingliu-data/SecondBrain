"""Orchestrator — Phase 3 top-level agent turn driver.

Replaces ``run_agent_loop`` as the entry point when chat routes want the
PMPA + multi-agent behaviour. Flow::

    build context → planner gate
        ├── gate off OR single-step plan  → inline action loop
        └── multi-step plan                → parallel workers + synthesizer

Multi-step turns emit additional SSE event types so clients can render
progress:

- ``event: plan``         — the full plan dict (as in Phase 2)
- ``event: step_start``   — a worker started a step  {step_id, worker_id, goal}
- ``event: step_done``    — a worker finished a step {step_id, state, summary_preview}
- ``event: token``        — final synthesizer token (same shape as today)
- ``event: done``         — terminates the stream

Simple turns emit only ``token`` / ``tool_call`` / ``avatar_command`` / ``done``
like the legacy loop, keeping client compatibility for non-orchestrated
turns.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from app.agent.loop import run_action_loop
from app.agent.plan import Plan, Step
from app.agent.planner import make_plan, should_plan
from app.agent.worker import (
    WorkerResult,
    render_worker_done_event,
    render_worker_start_event,
    run_step_as_worker,
)
from app.session.session_dir import SessionDir
from app.session.ticket import Ticket
from app.skills.registry import ToolSet
from app.user.context_builder import ContextBuilder

logger = logging.getLogger("orchestrator")

WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", 3))

SYNTHESIZER_SYSTEM = """You are a SYNTHESIZER. The user asked a question; a plan of steps
was executed by worker agents. You are given the original user message plus
each step's result summary. Produce the final answer for the user.

Rules:
- Answer in the voice of the main assistant (concise, mobile-chat style).
- Base your answer on the step summaries. Cite concrete facts from them.
- If a step failed, mention the gap plainly rather than pretending it succeeded.
- Do NOT repeat the plan back; the user doesn't need to see it.
- Keep it short — 1-3 sentences unless the content demands more.
"""


async def run_agent_turn(
    message: str,
    session: SessionDir,
    registry,
    llm,
):
    """Async generator — the post-Phase-2 entry point used by routes.

    Responsibilities:
    - Build context (system prompt + profile/memory)
    - Append the user message to history
    - Gate and optionally run the planner
    - Dispatch to inline or orchestrated execution
    """
    user_id = session.read_meta().get("user_id", "") or ""
    system = ContextBuilder(user_id=user_id, session=session).build_system(message)
    session.append_history("user", message)
    history = session.read_history(limit=20)

    tool_set: ToolSet = registry.build_tool_registry_for_session(
        session, message, scenario="authenticated"
    )

    plan: Plan | None = None
    if await should_plan(message, tool_set.tool_defs):
        plan_ticket = Ticket.start(
            session, "planner", inputs={"message_len": len(message)}
        )
        try:
            plan = await make_plan(llm, message, tool_set.tool_defs, session)
            plan_ticket.finish(
                "success", summary=f"{plan.plan_id}: {len(plan.steps)} steps"
            )
            yield f"event: plan\ndata: {json.dumps(plan.to_dict())}\n\n"
        except Exception as e:
            logger.warning(f"Planner error (continuing without plan): {e}")
            plan_ticket.finish("failed", summary=f"{type(e).__name__}: {e}")
            plan = None

    # Multi-step plans route to workers ONLY when every suggested tool is
    # server-side. Workers can't own the SSE stream that iOS device tools
    # need, so a plan mentioning calendar/reminders/etc. falls through to
    # the inline loop where the main stream is in scope.
    if (
        plan is not None
        and len(plan.steps) > 1
        and not _plan_touches_device_tools(plan, tool_set)
    ):
        async for ev in _run_orchestrated(
            plan=plan,
            user_message=message,
            base_system=system,
            session=session,
            registry=registry,
            tool_set=tool_set,
            llm=llm,
            user_id=user_id,
        ):
            yield ev
        return

    # Inline fast path: plan is None, or plan has one step. Inject the plan
    # block (if any) into the system prompt as advisory context.
    if plan is not None:
        system = system + "\n\n" + plan.as_prompt_block()
    messages = [{"role": "system", "content": system}] + history
    async for ev in run_action_loop(
        messages, session, registry, tool_set, llm, user_id,
        loop_tag="inline",
    ):
        yield ev


async def _run_orchestrated(
    *,
    plan: Plan,
    user_message: str,
    base_system: str,
    session: SessionDir,
    registry,
    tool_set: ToolSet,
    llm,
    user_id: str,
):
    """Run a multi-step plan: parallel workers in dep-waves + final synthesizer."""
    orch_ticket = Ticket.start(
        session,
        "orchestrator",
        inputs={"plan_id": plan.plan_id, "n_steps": len(plan.steps)},
    )
    sem = asyncio.Semaphore(WORKER_CONCURRENCY)
    summaries: dict[str, str] = {}

    try:
        while not plan.all_terminal():
            ready = plan.ready_steps()
            if not ready:
                # Deadlock: pending steps whose deps all failed. Mark them failed
                # so the loop terminates instead of spinning forever.
                stuck = [s for s in plan.steps if s.state == "pending"]
                for s in stuck:
                    s.state = "failed"
                    s.result_summary = "(skipped: upstream step failed)"
                    summaries[s.id] = s.result_summary
                break

            upstream = {sid: summaries[sid] for sid in summaries}

            async def _guarded(step: Step) -> WorkerResult:
                async with sem:
                    # Emit step_start before the worker begins so clients see
                    # concurrent starts as soon as the wave launches.
                    # (We can't yield from here — events are routed via a queue.)
                    step.state = "running"
                    return await run_step_as_worker(
                        step,
                        upstream_summaries=upstream,
                        tool_set=tool_set,
                        registry=registry,
                        llm=llm,
                        session=session,
                        user_id=user_id,
                    )

            # Emit step_start for every step in this wave immediately.
            for step in ready:
                yield (
                    "event: step_start\n"
                    f"data: {json.dumps({'step_id': step.id, 'goal': step.goal})}\n\n"
                )

            wave_results: list[WorkerResult] = await asyncio.gather(
                *[_guarded(s) for s in ready]
            )
            plan.save()

            for r in wave_results:
                summaries[r.step_id] = r.summary
                yield render_worker_done_event(r)

        # Synthesizer — one LLM call with the user's message + step summaries.
        synth_messages = [
            {"role": "system", "content": SYNTHESIZER_SYSTEM},
            {
                "role": "user",
                "content": _build_synth_user_content(
                    user_message, plan, summaries
                ),
            },
        ]
        # Use an empty ToolSet so the synthesizer can't call tools (tools=None
        # inside run_action_loop because tool_defs is empty).
        empty_tools = ToolSet(
            tool_defs=[], server_tool_names=set(), device_tool_names=set(),
            origin="synthesizer",
        )
        async for ev in run_action_loop(
            synth_messages,
            session,
            registry,
            empty_tools,
            llm,
            user_id,
            loop_tag="synthesizer",
        ):
            yield ev

        n_done = sum(1 for s in plan.steps if s.state == "done")
        n_failed = sum(1 for s in plan.steps if s.state == "failed")
        orch_ticket.finish(
            "success" if n_failed == 0 else "failed",
            summary=f"{plan.plan_id}: {n_done} done, {n_failed} failed",
        )
    except Exception as e:
        orch_ticket.finish("failed", summary=f"{type(e).__name__}: {e}")
        logger.exception("Orchestrator failed")
        # Don't swallow — let the route's ticket record the turn failure too.
        raise


def _plan_touches_device_tools(plan: Plan, tool_set: ToolSet) -> bool:
    """True if any step suggests a device tool. Such plans must run inline
    because workers can't route the SSE ``tool_call`` event to iOS."""
    device = tool_set.device_tool_names
    if not device:
        return False
    for step in plan.steps:
        if any(t in device for t in step.suggested_tools):
            return True
    return False


def _build_synth_user_content(
    user_message: str,
    plan: Plan,
    summaries: dict[str, str],
) -> str:
    lines = [f"User asked:\n{user_message}", "", "Plan step results:"]
    for s in plan.steps:
        summ = summaries.get(s.id) or s.result_summary or "(no output)"
        lines.append(f"- [{s.id}] ({s.state}) {s.goal}")
        lines.append(f"  → {summ[:600]}")
    lines.append("")
    lines.append("Produce the final answer for the user now.")
    return "\n".join(lines)
