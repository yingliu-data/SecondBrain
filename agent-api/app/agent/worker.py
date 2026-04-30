"""Worker — runs a single plan Step as an isolated mini-agent.

A worker's conversation is scoped to one step: its messages are discarded
after the worker returns, so the orchestrator's synthesizer only sees the
compact ``result_summary``, not the full transcript.

Phase 3 scope:
- Workers dispatch **server-side tools only**. Device tools require the main
  SSE stream to reach iOS, which workers don't own. Multi-step plans that
  reference device tools should mark those steps ``mode="inline"``; for now
  we just log and skip the dispatch rather than deadlock.
- Parallelism is provided by the orchestrator (``asyncio.gather`` +
  ``WORKER_CONCURRENCY`` semaphore). Each worker itself runs sequentially.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass

from app.agent.plan import Step
from app.agent.sanitize import sanitize
from app.config import MAX_TOOLS
from app.session.session_dir import SessionDir
from app.session.ticket import Ticket
from app.skills.registry import ToolSet
from app.util.json_safe import DefensiveJSONError, parse_json_defensive

logger = logging.getLogger("worker")


@dataclass
class WorkerResult:
    step_id: str
    worker_id: str
    state: str              # "done" | "failed"
    summary: str            # short text for synthesizer; also stored on the Step
    error: str | None = None


def _worker_id() -> str:
    return "w_" + uuid.uuid4().hex[:8]


def _step_tool_defs(step: Step, tool_set: ToolSet) -> list[dict]:
    """Narrow the tool list to what this step suggests (plus none else).

    If the step suggests no tools, returns an empty list — the LLM answers
    from context alone. If the step suggests tools that aren't in the
    registry, they're silently dropped (already filtered at plan time, but
    defensive).
    """
    if not step.suggested_tools:
        return []
    wanted = set(step.suggested_tools)
    return [t for t in tool_set.tool_defs if t["function"]["name"] in wanted]


def _build_worker_system(step: Step, upstream_summaries: dict[str, str]) -> str:
    """System prompt for a worker: focused on the step's goal.

    Worker output is consumed by the synthesizer, not the end user, so we
    ask for a dense summary rather than chat-style prose.
    """
    lines = [
        "You are a WORKER agent executing ONE step of a larger plan.",
        f"Step goal: {step.goal}",
        "",
        "Rules:",
        "- Use the provided tools when needed, then produce a FINAL text",
        "  summary of what you found or did.",
        "- Your summary is consumed by a synthesizer agent — keep it dense",
        "  and factual, no greetings or chit-chat.",
        "- Max ~150 words.",
    ]
    if upstream_summaries:
        lines.append("")
        lines.append("Results from prior steps (context):")
        for sid, summ in upstream_summaries.items():
            lines.append(f"- [{sid}] {summ[:240]}")
    return "\n".join(lines)


async def run_step_as_worker(
    step: Step,
    *,
    upstream_summaries: dict[str, str],
    tool_set: ToolSet,
    registry,
    llm,
    session: SessionDir,
    user_id: str,
) -> WorkerResult:
    """Execute one Step as a fresh LLM mini-conversation.

    Side effects:
    - Writes a per-worker ticket under ``session.tickets/``.
    - Calls server-side tools via ``registry.execute_server_tool`` (which
      publishes ``session`` + ``user_id`` through contextvars).
    - Does NOT touch ``session.history_jsonl`` — that belongs to the main
      conversation; the worker's transcript is ephemeral.

    Returns a ``WorkerResult`` regardless of outcome (failures produce a
    ``state="failed"`` result so the orchestrator can continue or abort
    deliberately).
    """
    worker_id = _worker_id()
    step.worker_id = worker_id
    step.state = "running"

    step_defs = _step_tool_defs(step, tool_set)
    server_tools = tool_set.server_tool_names
    device_tools = tool_set.device_tool_names

    ticket = Ticket.start(
        session,
        f"worker.{step.id}",
        inputs={"worker_id": worker_id, "goal": step.goal, "n_tools": len(step_defs)},
    )

    system = _build_worker_system(step, upstream_summaries)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": step.goal},
    ]

    final_text = ""
    try:
        for loop_idx in range(MAX_TOOLS):
            try:
                resp = await llm.chat_completion(messages, tools=step_defs or None)
            except Exception as e:
                raise RuntimeError(f"LLM failed in worker {worker_id}: {e}") from e

            choice = resp["choices"][0]
            assistant_msg = choice["message"]
            finish = choice.get("finish_reason", "stop")

            if finish == "tool_calls" and assistant_msg.get("tool_calls"):
                messages.append(assistant_msg)
                tool_calls = assistant_msg["tool_calls"]

                async def _dispatch(idx: int, tc: dict) -> tuple[str, str]:
                    tool_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        arguments = parse_json_defensive(raw_args, expect=dict)
                    except DefensiveJSONError:
                        arguments = {}
                    tc_id = tc.get("id", f"tc_{int(time.time()*1000)}_{idx}")

                    if tool_name in server_tools:
                        result = await registry.execute_server_tool(
                            tool_name, arguments, session=session, user_id=user_id,
                        )
                    elif tool_name in device_tools:
                        logger.warning(
                            f"Worker {worker_id} step {step.id} requested device tool "
                            f"{tool_name!r} — device tools are not supported in workers; "
                            "returning an error result so the LLM can recover."
                        )
                        result = (
                            f"Error: tool {tool_name!r} is not available in worker "
                            "context. Continue without it or note the gap."
                        )
                    else:
                        result = f"Error: Unknown tool '{tool_name}'."
                    return tc_id, sanitize(result)

                pairs = await asyncio.gather(
                    *[_dispatch(i, tc) for i, tc in enumerate(tool_calls)]
                )
                for tc_id, result in pairs:
                    messages.append(
                        {"role": "tool", "tool_call_id": tc_id, "content": result}
                    )
                continue

            final_text = (assistant_msg.get("content") or "").strip()
            break
        else:
            final_text = "(worker ran out of tool iterations without producing a result)"
    except Exception as e:
        step.state = "failed"
        step.result_summary = f"{type(e).__name__}: {e}"
        ticket.finish("failed", summary=step.result_summary)
        logger.exception(f"Worker {worker_id} step {step.id} failed")
        return WorkerResult(
            step_id=step.id, worker_id=worker_id, state="failed",
            summary=step.result_summary, error=str(e),
        )

    summary = final_text or "(worker produced empty summary)"
    step.state = "done"
    step.result_summary = summary[:800]
    ticket.finish("success", summary=f"{len(summary)} chars")
    return WorkerResult(
        step_id=step.id, worker_id=worker_id, state="done", summary=step.result_summary,
    )


def render_worker_start_event(result: WorkerResult, step: Step) -> str:
    return (
        "event: step_start\n"
        f"data: {json.dumps({'step_id': step.id, 'worker_id': result.worker_id, 'goal': step.goal})}\n\n"
    )


def render_worker_done_event(result: WorkerResult) -> str:
    preview = (result.summary or "")[:160]
    return (
        "event: step_done\n"
        f"data: {json.dumps({'step_id': result.step_id, 'state': result.state, 'summary_preview': preview})}\n\n"
    )
