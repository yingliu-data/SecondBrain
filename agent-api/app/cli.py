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


async def run_turn(tenant, session_id: str, text: str, out) -> None:
    """One input: its reply streamed to `out` as it arrives, an `error:` line per error."""
    from app.routes import chat
    async for item in await chat.open_turn(tenant, session_id, text):
        event, data = _sse(item)
        if event == "token":
            out.write(data["text"])
        elif event == "error":
            out.write(f"\nerror: {data.get('message')}\n")
        out.flush()
    out.write("\n")
    out.flush()


async def chat_loop(tenant, session_id: str, lines: asyncio.Queue, out) -> None:
    """A turn per input line until None (end of input); then, or on
    cancellation (Ctrl-C), every guest is parked and stopped."""
    from app.routes import chat
    try:
        while True:
            out.write("> ")
            out.flush()
            line = await lines.get()
            if line is None:
                out.write("\n")
                return
            if text := line.strip():
                await run_turn(tenant, session_id, text, out)
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
