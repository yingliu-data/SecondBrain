"""Chat with the agent harness in the terminal: the real agent loop
(Workers, open_turn, as POST /api/v1/chat) on the local sandbox and the
direct path (no VM, no Temporal), with the LLM vendor of the config file.

  cd agent-api && uv run python -m app.cli [--session NAME] [--config PATH] [--env-file PATH]

The repo `.env` is read first (as docker compose reads it; a variable already
set wins), so the vendor keys, API_SECRET_KEY, INTERNAL_TOKEN and MONGODB_*
need no export. Lines starting with `/` are commands (/help); a line ending
in `\\` continues on the next. On a terminal: line editing, history in
HISTORY_FILE, colours (not with NO_COLOR), a time and token footer per input.

`--session` resumes one from its log; /new and /resume switch sessions. End
of input or Ctrl-C parks every guest, then stops it (Workers.shutdown): no
guest process is left, and the next run resumes the session from its log. A
missing or bad config file exits 2.

app.config reads the environment at import, so app.* is imported only after
the environment is set.
"""
import argparse
import asyncio
import json
import os
import queue
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ENV = Path(__file__).resolve().parents[2] / ".env"
HISTORY_FILE = Path.home() / ".sb-cli-history"
PROMPT, MORE, ASK = "> ", "… ", "? "
PREVIEW = 160                           # characters of a tool result shown (not /verbose)
SESSIONS_SHOWN = 20

HELP = """\
commands:
  /help              this list
  /session           the current session id
  /new               start a new session
  /sessions          this tenant's sessions, newest first
  /resume ID|N       switch to a session (N: its number in /sessions)
  /tools             the tools the agent was given (after a first message)
  /verbose           show tool results in full (toggle)
  /quit              stop (also Ctrl-D, Ctrl-C)
a line ending in \\ continues on the next line"""


def _file_settings(path: str) -> dict:
    """The file's `settings`; {} when it cannot be read (app.config names the error)."""
    try:
        with open(path) as f:
            return json.load(f).get("settings", {})
    except (ValueError, AttributeError):
        return {}


def _load_env_file(path: Path) -> None:
    """The file's KEY=VALUE lines into the environment, read by python-dotenv
    (as ast_everything.store reads it: quotes dropped, inline `# comments`
    ignored, as docker compose does); a variable already set wins."""
    from dotenv import dotenv_values
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)


def _local_env(config_file: str) -> None:
    os.environ["CONFIG_FILE"] = config_file
    os.environ["HARNESS_SANDBOX"] = "local"
    os.environ["TEMPORAL_ADDRESS"] = ""
    os.environ.setdefault("API_SECRET_KEY", secrets.token_hex(16))
    if "MCP_GATEWAY_URL" not in _file_settings(config_file):
        os.environ.setdefault("MCP_GATEWAY_URL", "")


# ---------------------------------------------------------------- output

def _colour(out) -> bool:
    """Whether `out` is a terminal and NO_COLOR is unset."""
    isatty = getattr(out, "isatty", None)
    return not os.environ.get("NO_COLOR") and bool(isatty and isatty())


def _paint(out, code: str, text: str) -> str:
    """`text` in an ANSI style when _colour(out)."""
    return f"\033[{code}m{text}\033[0m" if _colour(out) else text


class Screen:
    """`out`, remembering whether the cursor is at the start of a line: guest
    events (_show_event) arrive in batches, in the middle of a reply's text."""

    def __init__(self, out):
        self.out, self.at_line_start = out, True

    def write(self, text: str) -> None:
        if text:
            self.out.write(text)
            self.at_line_start = text.endswith("\n")

    def flush(self) -> None:
        self.out.flush()

    def isatty(self) -> bool:
        return _colour(self.out)


def _line(out, text: str) -> None:
    """`text` on a line of its own."""
    out.write(("" if getattr(out, "at_line_start", False) else "\n") + text + "\n")


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class ChatState:
    """What the chat shows between turns: the session, its tools (from
    session_start), the input's usage (from settle) and its errors."""
    session_id: str
    errors: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    verbose: bool = False


def _sse(item: str) -> tuple[str | None, dict]:
    """(event, data) of one SSE item; (None, {}) for a keepalive comment."""
    event, data = None, {}
    for line in item.splitlines():
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return event, data


def _show_event(ev: dict, out, state: ChatState) -> None:
    """A `[tool]` line per tool call, its result (a preview, all of it with
    /verbose), an `error:` line per error event (its message also goes to
    `state.errors`); session_start and settle only update `state`."""
    body, kind = ev["body"], ev["type"]
    if kind == "session_start":
        state.tools = body.get("tools", [])
        return
    if kind == "settle":
        for k, v in body.get("usage", {}).items():
            if isinstance(v, int):
                state.usage[k] = state.usage.get(k, 0) + v
        return
    if kind == "tool_call":
        args = json.dumps(body["args"], ensure_ascii=False)
        _line(out, _paint(out, "36", f"[tool] {body['name']}({_short(args, 400)})"))
    elif kind == "tool_result":
        content = body["content"]
        if not state.verbose:
            content = _short(" ".join(content.split()), PREVIEW)
        _line(out, _paint(out, "2", f"  ↳ {content}"))
    elif kind == "compaction":
        _line(out, _paint(out, "2", f"[compacted {body['tokens_before']} → {body['tokens_after']} tokens]"))
    elif kind == "error":
        state.errors.append(body["message"])
        _line(out, _paint(out, "31", f"error: {body['message']}"))
    else:
        return
    out.flush()


def _footer(out, started: float, usage: dict) -> None:
    parts = [f"{time.monotonic() - started:.1f}s"]
    if usage:
        parts.append(f"{usage.get('prompt_tokens', 0):,} in / {usage.get('completion_tokens', 0):,} out tokens")
    _line(out, _paint(out, "2", f"({' · '.join(parts)})"))
    out.flush()


# ---------------------------------------------------------------- input

class Terminal:
    """Input lines from an interactive terminal: input() on a daemon thread
    (a read blocked on the terminal never holds up the exit), so readline
    line editing and history; it shows the prompt itself."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._asks: queue.Queue = queue.Queue()
        self._eof = False
        threading.Thread(target=self._read, daemon=True).start()

    async def get(self, prompt: str) -> str | None:
        if self._eof:
            return None
        fut = self._loop.create_future()
        self._asks.put((prompt, fut))
        line = await fut
        self._eof = line is None
        return line

    def _read(self) -> None:
        while True:
            prompt, fut = self._asks.get()
            try:
                line = input(prompt) + "\n"
            except EOFError:
                line = None
            self._loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(line))


async def _input(lines, out, prompt: str) -> str | None:
    """The next input line after `prompt`; None at end of input. A Terminal
    shows the prompt itself (readline needs its colours in \\001..\\002);
    a queue's goes to `out`."""
    if isinstance(lines, Terminal):
        return await lines.get(f"\001\033[1;36m\002{prompt}\001\033[0m\002"
                               if _colour(out) else prompt)
    out.write(prompt)
    out.flush()
    line = await lines.get()
    if line is None:
        lines.put_nowait(None)          # every later read sees the end of input too
    return line


async def _ask(question: dict, lines, out) -> str | None:
    """The answer to `question`: the next input line, a number picks an option."""
    options = question["options"]
    out.write("\n" + _paint(out, "1;33", question["text"]) + "\n")
    for i, option in enumerate(options, 1):
        out.write(f"  {i}. {option}\n")
    line = await _input(lines, out, ASK)
    if line is None:
        return None
    answer = line.strip()
    return options[int(answer) - 1] if answer.isascii() and answer.isdigit() and 0 < int(answer) <= len(options) else answer


# ---------------------------------------------------------------- chat

async def run_turn(tenant, session_id: str, text: str, lines, out,
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
            _line(out, _paint(out, "31", f"error: {data.get('message')}"))
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


async def _sessions(tenant) -> list[dict]:
    from app.routes import chat
    return await asyncio.to_thread(chat.workers.sessions.list_for_tenant, tenant.user, tenant.name)


async def _command(tenant, line: str, state: ChatState, out) -> bool:
    """Run one /command; False for /quit."""
    from app.session.ids import is_safe_id, make_session_id
    name, _, arg = line[1:].partition(" ")
    arg = arg.strip()
    say = lambda text: out.write(text + "\n")      # noqa: E731
    if name in ("quit", "exit", "q"):
        return False
    if name in ("help", "h", "?"):
        say(HELP)
    elif name == "session":
        say(f"session: {state.session_id}")
    elif name == "new":
        state.session_id, state.tools = make_session_id(), []
        say(f"session: {state.session_id}")
    elif name == "sessions":
        rows = await _sessions(tenant)
        for i, row in enumerate(rows[:SESSIONS_SHOWN], 1):
            mark = "*" if row["session_id"] == state.session_id else " "
            title = f"  {_short(row['title'], 50)}" if row["title"] else ""
            say(f"{mark}{i:>3}. {row['session_id']}  {_paint(out, '2', str(row['updated_at'] or ''))}{title}")
        if len(rows) > SESSIONS_SHOWN:
            say(_paint(out, "2", f"  … {len(rows) - SESSIONS_SHOWN} older (/resume ID)"))
        if not rows:
            say("no sessions yet")
    elif name == "resume":
        if arg.isdigit():
            rows = await _sessions(tenant)
            arg = rows[int(arg) - 1]["session_id"] if 0 < int(arg) <= len(rows) else ""
        if not arg or not is_safe_id(arg):
            say(_paint(out, "31", "usage: /resume ID, or /resume N (a number from /sessions)"))
        else:
            state.session_id, state.tools = arg, []
            say(f"session: {state.session_id}")
    elif name == "tools":
        if not state.tools:
            say("no tools known yet: send a message first")
        for tool in state.tools:
            say(f"  {tool['name']}  {_paint(out, '2', tool.get('side') or '')}")
    elif name == "verbose":
        state.verbose = not state.verbose
        say(f"verbose tool results: {'on' if state.verbose else 'off'}")
    else:
        say(_paint(out, "31", f"unknown command /{name} (/help lists them)"))
    out.flush()
    return True


async def chat_loop(tenant, session_id: str, lines, out) -> None:
    """A turn per input line until None (end of input) or /quit; then, or on
    cancellation (Ctrl-C), every guest is parked and stopped. `lines` is a
    Terminal or an asyncio.Queue of lines."""
    from app.routes import chat
    state, out = ChatState(session_id), Screen(out)
    chat.workers.worker_kwargs["on_event"] = lambda ev: _show_event(ev, out, state)
    try:
        while True:
            line = await _input(lines, out, PROMPT)
            while line is not None and line.rstrip("\n").endswith("\\"):
                more = await _input(lines, out, MORE)
                line = line.rstrip("\n")[:-1] + "\n" + (more or "")
            if line is None:
                out.write("\n")
                return
            text = line.strip()
            if text.startswith("/"):
                if not await _command(tenant, text, state, out):
                    return
            elif text:
                state.errors.clear()
                state.usage = {}
                started = time.monotonic()
                await run_turn(tenant, state.session_id, text, lines, out, state.errors)
                _footer(out, started, state.usage)
    finally:
        await chat.workers.shutdown()


def _read_stdin(loop: asyncio.AbstractEventLoop, lines: asyncio.Queue) -> None:
    for line in sys.stdin:
        loop.call_soon_threadsafe(lines.put_nowait, line)
    loop.call_soon_threadsafe(lines.put_nowait, None)


async def _run(tenant, session_id: str, interactive: bool) -> None:
    loop = asyncio.get_running_loop()
    if interactive:
        lines = Terminal(loop)
    else:
        lines = asyncio.Queue()
        # a daemon thread: a read blocked on stdin never holds up the exit
        threading.Thread(target=_read_stdin, args=(loop, lines), daemon=True).start()
    await chat_loop(tenant, session_id, lines, sys.stdout)


def _readline():
    """readline with the history loaded; None when there is none (Windows)."""
    try:
        import readline
    except ImportError:
        return None
    readline.set_history_length(1000)
    try:
        readline.read_history_file(HISTORY_FILE)
    except OSError:
        pass
    return readline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli",
                                     description="Chat with the agent harness in the terminal.")
    parser.add_argument("--session", help="the session id to resume (default: a new session)")
    parser.add_argument("--config", default=os.environ.get("CONFIG_FILE", "data/config.json"),
                        help="the config file (default: data/config.json)")
    parser.add_argument("--env-file", type=Path,
                        help=f"a KEY=VALUE file read into the environment (default: {REPO_ENV}, when it exists)")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.config):
        print(f"no config file at {args.config} (copy config.json.example there)", file=sys.stderr)
        return 2
    if args.env_file is not None and not args.env_file.is_file():
        print(f"no env file at {args.env_file}", file=sys.stderr)
        return 2
    env_file = args.env_file or (REPO_ENV if REPO_ENV.is_file() else None)
    if env_file is not None:
        _load_env_file(env_file)
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
    print(_paint(sys.stdout, "2", "/help for commands, Ctrl-D to quit"))

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    readline = _readline() if interactive else None
    if interactive:
        import termios
        tty = termios.tcgetattr(sys.stdin.fileno())
    try:
        asyncio.run(_run(tenant, session_id, interactive))
    except KeyboardInterrupt:
        print()
    finally:
        if interactive:
            # Ctrl-C can stop the run inside readline, which leaves the terminal raw
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tty)
        if readline is not None:
            try:
                readline.write_history_file(HISTORY_FILE)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
