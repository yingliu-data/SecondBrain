import json, asyncio, time, logging
from datetime import datetime
from app.config import (LLM_ENABLE_THINKING, MAX_TOOL_ROUNDS, SYSTEM_PROMPT,
                        TOOL_TIMEOUT)
from app.agent.sanitize import sanitize
from app.agent.enums import FinishReason, ToolOutcome

logger = logging.getLogger("agent")

# Shared state for tool result handoff (iPhone → agent loop)
tool_result_events: dict[str, asyncio.Event] = {}
tool_results: dict[str, str] = {}

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-store, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# Avatar tools that emit a separate SSE event for the frontend renderer.
_AVATAR_TOOLS = {"set_pose", "move_joints", "animate_sequence", "plan_movement"}


_OUTCOME_WORDS = {
    ToolOutcome.OK: "ok",
    ToolOutcome.ERROR: "failed",
    ToolOutcome.TIMEOUT: "timed out",
    ToolOutcome.INVALID_ARGUMENTS: "not run, bad arguments",
}

_EXHAUSTED_NOTE = (
    "[SYSTEM NOTE] The tool budget is exhausted; no more tool "
    "calls are possible. Tell the user whether their request "
    "succeeded or failed, with a one-to-two sentence summary "
    "of the tool results above.")

_TRUNCATED_NOTE = (
    "[SYSTEM NOTE] Your previous reply was cut off by the token limit and has "
    "been discarded, so any tool calls in it were NOT run. Answer the user "
    "directly and briefly, without calling tools.")


def _tool_outcome_line(tool_runs: list[dict]) -> str:
    """One-line deterministic summary of tool outcomes, used when the LLM
    can't produce a final answer itself. Plain words (no symbols) so TTS
    reads it cleanly."""
    if not tool_runs:
        return "No tools were run."
    parts = [f"{r['name']} ({_OUTCOME_WORDS.get(r['outcome'], 'failed')})"
             for r in tool_runs]
    return "Tools run: " + ", ".join(parts) + "."


async def run_agent_loop(message: str, history: list, registry, llm, *,
                         system_prompt: str | None = None,
                         max_tools: int | None = None,
                         max_tokens: int | None = None,
                         allowed_skills: set[str] | None = None,
                         trace=None):
    """Generator that yields SSE events.
    - registry: routes tool calls to the right skill
    - llm: LLMProvider instance (local, cloud, or fallback chain)
    - trace: optional callable(event, detail=None, duration_ms=None) recording
      tool calls/results/errors to the session's trace log
    Tenant overrides default to None = legacy global behavior."""

    if trace is None:
        def trace(event, detail=None, duration_ms=None):
            pass

    # System prompt may already be fully composed (ContextBuilder); only
    # format if the {current_time} placeholder is present.
    system = system_prompt or SYSTEM_PROMPT
    if "{current_time}" in system:
        system = system.format(current_time=datetime.now().isoformat())
    # Rounds of the agent loop, not the number of tools offered (config.py).
    tool_budget = max_tools or MAX_TOOL_ROUNDS
    history.append({"role": "user", "content": message})
    messages = [{"role": "system", "content": system}] + history[-20:]

    # Get tool definitions filtered by query relevance (not all tools)
    tool_defs = registry.get_tools_for_query(message, allowed_skills)
    server_tools = registry.get_server_tool_names(allowed_skills)
    device_tools = registry.get_device_tool_names(allowed_skills)
    tool_runs: list[dict] = []  # [{name, ok}] for end-of-turn outcome summaries

    async def wrap_up(note: str, loops: int):
        """Shared end-of-turn path: one no-tools LLM call for a real outcome
        summary, falling back to a deterministic line. Used both when the tool
        budget is exhausted and when a response was truncated."""
        text = ""
        try:
            messages.append({"role": "user", "content": note})
            resp = await llm.chat_completion(
                messages, max_tokens=max_tokens,
                chat_template_kwargs=(
                    None if LLM_ENABLE_THINKING else {"enable_thinking": False}),
            )
            text = (resp["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            logger.error(f"Wrap-up summary LLM call failed: {e}")
            trace("llm_error", {"error": str(e)[:500], "stage": "wrap_up"})
        if not text:
            text = ("I wasn't able to complete that request. "
                    + _tool_outcome_line(tool_runs))
        trace("assistant_response",
              {"chars": len(text), "loops": loops, "wrap_up": True})
        history.append({"role": "assistant", "content": text})
        for word in text.split(" "):
            yield f"event: token\ndata: {json.dumps({'text': word + ' '})}\n\n"
        yield "event: done\ndata: {}\n\n"

    for loop_idx in range(tool_budget):
        # Call LLM via provider abstraction (local → cloud fallback)
        try:
            resp = await llm.chat_completion(
                messages, tools=tool_defs or None, max_tokens=max_tokens,
                chat_template_kwargs=(
                    None if LLM_ENABLE_THINKING else {"enable_thinking": False}),
            )
        except Exception as e:
            logger.error(f"LLM unavailable after retries: {e}")
            trace("llm_error", {"error": str(e)[:500]})
            error_msg = "I'm temporarily unable to respond — the server may be restarting. Please try again in a moment."
            if tool_runs:
                error_msg = ("I ran into a problem before finishing. "
                             + _tool_outcome_line(tool_runs)
                             + " Please try again in a moment.")
            history.append({"role": "assistant", "content": error_msg})
            yield f"event: token\ndata: {json.dumps({'text': error_msg})}\n\n"
            yield "event: done\ndata: {}\n\n"
            return
        choice = resp["choices"][0]
        assistant_msg = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        # ── Truncated response ──────────────────────────
        # The token cap cut generation off mid-message. Discard it entirely
        # rather than appending it: a half-generated message can carry partial
        # tool_calls, and appending it would leave dangling tool_call ids with
        # no matching tool results, which is a malformed request on the next
        # round. Re-ask without tools so the user gets a coherent answer.
        if finish_reason == FinishReason.LENGTH:
            logger.warning(f"Response truncated at token limit [loop {loop_idx+1}]")
            trace("truncated_response", {"loop": loop_idx + 1})
            async for event in wrap_up(_TRUNCATED_NOTE, loop_idx + 1):
                yield event
            return

        # ── Tool calls ──────────────────────────────────
        if finish_reason == FinishReason.TOOL_CALLS and assistant_msg.get("tool_calls"):
            messages.append(assistant_msg)

            for tc in assistant_msg["tool_calls"]:
                tool_name = tc["function"]["name"]
                tc_id = tc.get("id", f"tc_{int(time.time()*1000)}")
                outcome = None  # set explicitly below, else derived from result

                # Fail CLOSED on unparseable arguments. This previously fell back
                # to `arguments = {}` and ran the tool anyway -- so a call whose
                # argument JSON the model truncated executed with NO arguments at
                # all (a create_calendar_event with no title, date or calendar).
                # vLLM's tool parsers do emit finish_reason="tool_calls" alongside
                # partial argument JSON, so this is reachable in normal operation,
                # and a silently argument-less side effect is worse than a failed
                # one. The model sees the error and can re-issue the call.
                raw_args = tc["function"].get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_args)
                    if not isinstance(arguments, dict):
                        raise ValueError(
                            f"expected a JSON object, got {type(arguments).__name__}")
                except (json.JSONDecodeError, ValueError) as e:
                    result = (f"Error: arguments for '{tool_name}' were not valid JSON "
                              f"({e}). The tool was NOT run. Re-issue the call with "
                              f"well-formed arguments.")
                    logger.warning(
                        f"Malformed tool arguments for {tool_name}: {str(raw_args)[:200]!r}")
                    trace("tool_arguments_invalid",
                          {"name": tool_name, "raw": str(raw_args)[:500]})
                    tool_runs.append({"name": tool_name,
                                      "outcome": ToolOutcome.INVALID_ARGUMENTS})
                    messages.append({"role": "tool", "tool_call_id": tc_id,
                                     "content": result})
                    continue

                logger.info(f"Tool call: {tool_name}({arguments}) [loop {loop_idx+1}]")
                trace("tool_call", {"name": tool_name, "arguments": arguments,
                                    "loop": loop_idx + 1})
                tool_started = time.monotonic()

                if tool_name in server_tools:
                    # Server skill — execute via registry. Long-running tools
                    # (e.g. remote MCP pipelines) run as a task while we emit
                    # SSE comment keepalives so proxies don't drop the stream.
                    exec_task = asyncio.ensure_future(
                        registry.execute_server_tool(tool_name, arguments, allowed_skills))
                    while True:
                        done, _ = await asyncio.wait({exec_task}, timeout=15)
                        if done:
                            break
                        yield ": keepalive\n\n"
                    result = exec_task.result()

                    # Emit avatar commands as a separate SSE event so the
                    # frontend can act on them before the LLM text response.
                    if tool_name in _AVATAR_TOOLS:
                        try:
                            parsed = json.loads(result)
                            yield f"event: avatar_command\ndata: {json.dumps({'name': tool_name, 'result': parsed})}\n\n"
                            # For plan_movement, feed compact summary to LLM
                            # instead of full frame data to save context tokens.
                            if tool_name == "plan_movement":
                                n_frames = len(parsed.get("frames", []))
                                result = json.dumps({
                                    "status": "ok",
                                    "frames_generated": n_frames,
                                    "loop": parsed.get("loop", False),
                                })
                        except (json.JSONDecodeError, TypeError):
                            pass
                elif tool_name in device_tools:
                    # Device skill — delegate to iPhone via SSE
                    yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': tool_name, 'arguments': arguments})}\n\n"

                    evt = asyncio.Event()
                    tool_result_events[tc_id] = evt
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=TOOL_TIMEOUT)
                        result = tool_results.pop(tc_id, "No result")
                    except asyncio.TimeoutError:
                        result = f"Error: {tool_name} timed out after {TOOL_TIMEOUT}s. The iPhone may be unreachable."
                        outcome = ToolOutcome.TIMEOUT
                    finally:
                        # Both dicts, not just one: a device reply arriving after
                        # the timeout inserts into tool_results and, without this,
                        # the entry is never removed -- an unbounded leak in a
                        # long-lived process.
                        tool_result_events.pop(tc_id, None)
                        tool_results.pop(tc_id, None)
                else:
                    all_tools = server_tools | device_tools
                    result = f"Error: Unknown tool '{tool_name}'. Available: {', '.join(all_tools)}"

                result = sanitize(result)
                if outcome is None:
                    outcome = (ToolOutcome.ERROR if result.startswith("Error")
                               else ToolOutcome.OK)
                tool_runs.append({"name": tool_name, "outcome": outcome})
                trace("tool_result",
                      {"name": tool_name, "result": result[:2000]},
                      duration_ms=int((time.monotonic() - tool_started) * 1000))
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
            continue

        # ── Final text response ─────────────────────────
        text = (assistant_msg.get("content") or "").strip()
        if not text:
            logger.warning("LLM returned empty final content")
            text = ("I finished but couldn't produce an answer. "
                    + _tool_outcome_line(tool_runs))
        trace("assistant_response", {"chars": len(text), "loops": loop_idx + 1})
        history.append({"role": "assistant", "content": text})
        for word in text.split(" "):
            yield f"event: token\ndata: {json.dumps({'text': word + ' '})}\n\n"
        yield "event: done\ndata: {}\n\n"
        return
    else:
        # Tool loop exhausted without a final text response. One last no-tools
        # LLM call so the user still gets a real outcome summary.
        logger.warning(f"Agent loop exhausted {tool_budget} tool iterations without final response")
        trace("loop_exhausted", {"budget": tool_budget})
        async for event in wrap_up(_EXHAUSTED_NOTE, tool_budget):
            yield event
