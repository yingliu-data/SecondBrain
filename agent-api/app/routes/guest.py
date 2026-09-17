"""Guest chat endpoint — unauthenticated, avatar_control only."""

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from app.agent.loop import SSE_HEADERS, _AVATAR_TOOLS
from app.agent.sanitize import sanitize
from app.auth.guest import (
    client_ip,
    guest_manager,
    GUEST_SYSTEM_PROMPT,
    ALLOWED_ORIGINS,
    MAX_INPUT_CHARS,
    MAX_OUTPUT_TOKENS,
)

logger = logging.getLogger("guest")
sec_log = logging.getLogger("security")
router = APIRouter()

_registry = None
_llm = None


def set_dependencies(registry, llm):
    global _registry, _llm
    _registry = registry
    _llm = llm


@router.post("/api/v1/guest/chat")
async def guest_chat(request: Request):
    ip = client_ip(request)

    # Origin check — best-effort CSRF hint, NOT an authentication control.
    # The Origin header is supplied by the caller and the production proxy
    # forwards it verbatim, so any non-browser client (curl) can set it to an
    # allowed value. The real control is proxy-side: pinning the upstream
    # Origin in pose-spatial-studio. Keep this as a cheap filter for casual
    # cross-site use and as an abuse signal — never rely on it for auth.
    origin = request.headers.get("origin", "")
    if origin not in ALLOWED_ORIGINS:
        sec_log.warning(f"GUEST_ORIGIN_REJECT ip={ip} origin={origin!r}")
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    body = await request.json()
    message = body.get("message", "")[:MAX_INPUT_CHARS]
    session_id = body.get("session_id", "")

    if not session_id or not message.strip():
        return JSONResponse({"error": "Missing session_id or message"}, status_code=400)

    session, error = guest_manager.get_or_create(session_id, ip)
    if not session:
        return JSONResponse({"error": error}, status_code=429)

    session.message_count += 1

    async def generate():
        # Get only avatar_control tools
        avatar_skill = _registry._skills.get("avatar_control")
        if not avatar_skill:
            yield f"event: token\ndata: {json.dumps({'text': 'Avatar control is not available.'})}\n\n"
            yield "event: done\ndata: {}\n\n"
            return

        tool_defs = avatar_skill.get_tool_definitions()
        tool_names = {t["function"]["name"] for t in tool_defs}

        system = GUEST_SYSTEM_PROMPT.format(current_time=datetime.now().isoformat())
        session.history.append({"role": "user", "content": message})
        messages = [{"role": "system", "content": system}] + session.history[-10:]

        # Single tool call iteration (guests get 1 tool round max)
        for _ in range(2):
            try:
                resp = await _llm.chat_completion(
                    messages,
                    tools=tool_defs,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    # Same reason as the main agent loop (commit 58d66e3): with
                    # thinking on, Qwen3 describes tools instead of calling them
                    # -- fatal here, where every request is a tool request. It
                    # also spends the MAX_OUTPUT_TOKENS budget on reasoning
                    # traces and returns empty content.
                    chat_template_kwargs={"enable_thinking": False},
                )
            except Exception as e:
                logger.error(f"Guest LLM error: {e}")
                yield f"event: token\ndata: {json.dumps({'text': 'Service temporarily unavailable.'})}\n\n"
                yield "event: done\ndata: {}\n\n"
                return

            choice = resp["choices"][0]
            assistant_msg = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            if finish_reason == "tool_calls" and assistant_msg.get("tool_calls"):
                messages.append(assistant_msg)

                for tc in assistant_msg["tool_calls"]:
                    tool_name = tc["function"]["name"]
                    try:
                        arguments = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        arguments = {}
                    tc_id = tc.get("id", "guest_tc")

                    logger.info(f"Guest tool call: {tool_name}({arguments})")

                    if tool_name not in tool_names:
                        result = f"Error: Tool '{tool_name}' not available in guest mode."
                    else:
                        result = await _registry.execute_server_tool(tool_name, arguments)

                        # Emit avatar_command SSE event
                        if tool_name in _AVATAR_TOOLS:
                            try:
                                parsed = json.loads(result)
                                yield f"event: avatar_command\ndata: {json.dumps({'name': tool_name, 'result': parsed})}\n\n"
                                # Compact summary for LLM context
                                if tool_name == "plan_movement":
                                    n_frames = len(parsed.get("frames", []))
                                    result = json.dumps({
                                        "status": "ok",
                                        "frames_generated": n_frames,
                                        "loop": parsed.get("loop", False),
                                    })
                            except (json.JSONDecodeError, TypeError):
                                pass

                    result = sanitize(result)
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                continue

            # Final text response
            # vLLM sends content: null (key present -> .get(k, "") yields
            # None, and None.split() kills the stream with no done event).
            text = assistant_msg.get("content") or ""
            session.history.append({"role": "assistant", "content": text})
            for word in text.split(" "):
                yield f"event: token\ndata: {json.dumps({'text': word + ' '})}\n\n"
            yield "event: done\ndata: {}\n\n"
            return

        # Loop exhausted
        yield f"event: token\ndata: {json.dumps({'text': 'Could not complete request.'})}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_HEADERS)
