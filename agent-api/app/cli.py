"""Chat with the agent harness in the terminal: the real agent loop
(Workers, open_turn, as POST /api/v1/chat) on the local sandbox and the
direct path (no VM, no Temporal), with the LLM vendor of the config file.

  cd agent-api && uv run python -m app.cli [--session NAME] [--config PATH]

One session for the whole run; `--session` resumes one from its log. End of
input or Ctrl-C parks every guest, then stops it (Workers.shutdown): no guest
process is left, and the next run resumes the session from its log. A missing
or bad config file exits 2.

app.config reads the environment at import, so app.* is imported only after
the environment is set.
"""
import argparse
import asyncio
import json
import os
import secrets
import sys
import threading


def _file_settings(path: str) -> dict:
    """The file's `settings`; {} when it cannot be read (app.config names the error)."""
    try:
        with open(path) as f:
            return json.load(f).get("settings", {})
    except (ValueError, AttributeError):
        return {}


def _local_env(config_file: str) -> None:
    os.environ["CONFIG_FILE"] = config_file
    os.environ["HARNESS_SANDBOX"] = "local"
    os.environ["TEMPORAL_ADDRESS"] = ""
    os.environ.setdefault("API_SECRET_KEY", secrets.token_hex(16))
    if "MCP_GATEWAY_URL" not in _file_settings(config_file):
        os.environ.setdefault("MCP_GATEWAY_URL", "")


def _sse(item: str) -> tuple[str | None, dict]:
    """(event, data) of one SSE item; (None, {}) for a keepalive comment."""
    event, data = None, {}
    for line in item.splitlines():
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return event, data


def _show_event(ev: dict, out, errors: list) -> None:
    """A `[tool]` line per tool call and an `error:` line per error event (its
    message also goes to `errors`)."""
    body = ev["body"]
    if ev["type"] == "tool_call":
        out.write(f"\n[tool] {body['name']}({json.dumps(body['args'])})\n")
    elif ev["type"] == "error":
        errors.append(body["message"])
        out.write(f"\nerror: {body['message']}\n")
    else:
        return
    out.flush()


async def _ask(question: dict, lines: asyncio.Queue, out) -> str | None:
    """The answer to `question`: the next input line, a number picks an option."""
    options = question["options"]
    out.write(f"\n{question['text']}\n")
    for i, option in enumerate(options, 1):
        out.write(f"  {i}. {option}\n")
    out.write("? ")
    out.flush()
    line = await lines.get()
    if line is None:
        lines.put_nowait(None)          # chat_loop still sees the end of input
        return None
    answer = line.strip()
    return options[int(answer) - 1] if answer.isdigit() and 0 < int(answer) <= len(options) else answer


async def run_turn(tenant, session_id: str, text: str, lines: asyncio.Queue, out,
                   errors: list = (), answer_to: str | None = None) -> None:
    """One input: its reply streamed to `out` as it arrives. The stream's error
    is shown when no error event of the turn (`errors`, _show_event) already
    gave its cause. A question takes its answer from `lines`, and that
    answer's stream carries the rest of the task."""
    from app.routes import chat
    stream = await chat.open_turn(tenant, session_id, text, answer_to=answer_to)
    question = None
    async for item in stream:
        event, data = _sse(item)
        if event == "token":
            out.write(data["text"])
        elif event == "error" and not errors:
            out.write(f"\nerror: {data.get('message')}\n")
        elif event == "question" and data["question_id"] != answer_to:   # attach re-sends the open one
            question = data
            break
        out.flush()
    await stream.aclose()
    if question is None:
        out.write("\n")
        out.flush()
        return
    answer = await _ask(question, lines, out)
    if answer is not None:
        await run_turn(tenant, session_id, answer, lines, out, errors, question["question_id"])


async def chat_loop(tenant, session_id: str, lines: asyncio.Queue, out) -> None:
    """A turn per input line until None (end of input); then, or on
    cancellation (Ctrl-C), every guest is parked and stopped."""
    from app.routes import chat
    errors: list[str] = []
    chat.workers.worker_kwargs["on_event"] = lambda ev: _show_event(ev, out, errors)
    try:
        while True:
            out.write("> ")
            out.flush()
            line = await lines.get()
            if line is None:
                out.write("\n")
                return
            if text := line.strip():
                errors.clear()
                await run_turn(tenant, session_id, text, lines, out, errors)
    finally:
        await chat.workers.shutdown()


def _read_stdin(loop: asyncio.AbstractEventLoop, lines: asyncio.Queue) -> None:
    for line in sys.stdin:
        loop.call_soon_threadsafe(lines.put_nowait, line)
    loop.call_soon_threadsafe(lines.put_nowait, None)


async def _run(tenant, session_id: str) -> None:
    lines: asyncio.Queue = asyncio.Queue()
    # a daemon thread: a read blocked on the terminal never holds up the exit
    threading.Thread(target=_read_stdin, args=(asyncio.get_running_loop(), lines),
                     daemon=True).start()
    await chat_loop(tenant, session_id, lines, sys.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli",
                                     description="Chat with the agent harness in the terminal.")
    parser.add_argument("--session", help="the session id to resume (default: a new session)")
    parser.add_argument("--config", default=os.environ.get("CONFIG_FILE", "data/config.json"),
                        help="the config file (default: data/config.json)")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.config):
        print(f"no config file at {args.config} (copy config.json.example there)", file=sys.stderr)
        return 2
    _local_env(args.config)
    try:
        from app import config
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    from app.routes import chat
    from app.session.ids import is_safe_id, make_session_id
    from app.skills.registry import SkillRegistry
    from app.tenants import create_tenant_registry

    session_id = args.session or make_session_id()
    if not is_safe_id(session_id):
        print(f"invalid session id: {session_id}", file=sys.stderr)
        return 2
    tenant = create_tenant_registry().get_by_api_key(config.API_SECRET_KEY)
    chat.set_registry(SkillRegistry())
    print(f"{config.LLM_VENDOR} {config.LLM_MODEL} {config.LLM_URL}")
    print(f"session: {session_id}")
    try:
        asyncio.run(_run(tenant, session_id))
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
