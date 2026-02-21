import json, asyncio, time, logging
from datetime import datetime
from app.config import MAX_TOOLS, SYSTEM_PROMPT, TOOL_TIMEOUT
from app.agent.sanitize import sanitize

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


async def run_agent_loop(message: str, history: list, registry, llm):
    """Generator that yields SSE events.
    - registry: routes tool calls to the right skill
    - llm: LLMProvider instance (local, cloud, or fallback chain)"""

    system = SYSTEM_PROMPT.format(current_time=datetime.now().isoformat())
    history.append({"role": "user", "content": message})
    messages = [{"role": "system", "content": system}] + history[-20:]

    # Get tool definitions filtered by query relevance (not all tools)
    tool_defs = registry.get_tools_for_query(message)
    server_tools = registry.get_server_tool_names()
    device_tools = registry.get_device_tool_names()

    for loop_idx in range(MAX_TOOLS):
        # Call LLM via provider abstraction (local → cloud fallback)
        resp = await llm.chat_completion(messages, tools=tool_defs or None)
        choice = resp["choices"][0]
        assistant_msg = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        # ── Tool calls ──────────────────────────────────
        if finish_reason == "tool_calls" and assistant_msg.get("tool_calls"):
            messages.append(assistant_msg)

            for tc in assistant_msg["tool_calls"]:
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}
                tc_id = tc.get("id", f"tc_{int(time.time()*1000)}")

                logger.info(f"Tool call: {tool_name}({arguments}) [loop {loop_idx+1}]")

                if tool_name in server_tools:
                    # Server skill — execute via registry
                    result = await registry.execute_server_tool(tool_name, arguments)
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
                    finally:
                        tool_result_events.pop(tc_id, None)
                else:
                    all_tools = server_tools | device_tools
                    result = f"Error: Unknown tool '{tool_name}'. Available: {', '.join(all_tools)}"

                result = sanitize(result)
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
            continue

        # ── Final text response ─────────────────────────
        text = assistant_msg.get("content", "")
        history.append({"role": "assistant", "content": text})
        for word in text.split(" "):
            yield f"event: token\ndata: {json.dumps({'text': word + ' '})}\n\n"
        yield "event: done\ndata: {}\n\n"
        break
