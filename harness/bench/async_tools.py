"""bench.async_tools — the async tool-call measurement
(docs/ASYNC-TOOLS.md).

    python -m bench.async_tools --llm-url http://127.0.0.1:8000 --model Qwen/Qwen3-14B \
        [--tasks bench/tasks.json] [--runs N] [--only id,id] [--json] [--out results.json]
    python -m bench.async_tools --fake          # scripted LLM (CI smoke)

Every task of the fixed set runs through the real guest (`python -m
guest.main` as a subprocess, as tests/test_guest_e2e.py does) against the
fake host (tests/fakes/fake_host.py) over Unix-socket doubles of the vsock
ports, once with `async_tools: false` and once with `async_tools: true`
(per --runs). The workspace is seeded fresh from tasks.json for every run.

The LLM on :5010 is either
  - --fake: tests/fakes/fake_llm.FakeLLM scripted from each task's `fake`
    entry (`fake_script`): one response with the task's parallel calls plus
    FAKE_SLOW_CALL (a bash call that outlasts the fake grace window, so the
    `on` rows wake on a placeholder), then, per request, "waiting" while the
    request still shows a [pending] call whose late result it does not
    carry, else the answer; or
  - --llm-url/--model: a forwarding proxy that POSTs the guest's request
    to <llm-url>/v1/chat/completions (an OpenAI-compatible server: vLLM),
    non-streamed, the way the host's LLM proxy does.

Per run it records from the delivered event log (folded with protocol.fold,
which must replay clean): pass (the task's checker on the settle answer and
outcome done), outcome, turns, prompt/completion tokens, tool calls, the
widest turn (calls in one response), tool errors, placeholders committed
(async mode in effect) and wall time (task sent -> settle delivered).

Checker: {"contains_all": [...]} — every value must occur in the answer,
case-insensitively, as a whole token (digits are not matched inside longer
numbers).
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # harness/
DEFAULT_TASKS = ROOT / "bench" / "tasks.json"
SID = "sess-bench"
USER, TENANT = "bench-user", "bench"
DEFAULT_GRACE_S = 2.0              # limits.grace_s for a real model
FAKE_GRACE_S = 0.25                # --fake: short, and well below the slow call
FAKE_SLOW_CALL = ["bash", {"command": "sleep 0.75; echo slow lookup done"}]
FAKE_WAITING = "Waiting for the remaining lookups."
SYSTEM_PROMPT = ("You are a careful assistant working in a project workspace. Use the tools "
                 "to look things up; when several lookups are independent, request them "
                 "together in one response. Answer briefly and include the exact values. "
                 "{current_time}")


# ---------------------------------------------------------------- tasks
def load_tasks(path=DEFAULT_TASKS) -> dict:
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    ids = [t["id"] for t in spec["tasks"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate task ids in {path}")
    return spec


def seed_workspace(workspace: Path, files: dict) -> None:
    for rel, content in files.items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _token_re(value: str) -> re.Pattern:
    left = r"(?<![\w.])" if value[:1].isalnum() else ""
    right = r"(?![\w])" if value[-1:].isalnum() else ""
    return re.compile(left + re.escape(value) + right, re.IGNORECASE)


def check(spec: dict, answer: str | None) -> tuple[bool, list[str]]:
    """(passed, missing values)."""
    answer = answer or ""
    missing = [v for v in spec.get("contains_all", []) if not _token_re(str(v)).search(answer)]
    return not missing, missing


# ---------------------------------------------------------------- the LLM side
def still_pending(messages: list) -> set:
    """The call_ids a request shows as [pending] without their late result
    (guest.context's late message names the call: "[late tool result]
    call_id: <id>, ...")."""
    from protocol.fold import PLACEHOLDER
    pending = {m.get("tool_call_id") for m in messages
               if m.get("role") == "tool" and m.get("content") == PLACEHOLDER}
    for m in messages:
        content = m.get("content") if m.get("role") == "user" else None
        if isinstance(content, str) and content.startswith("[late tool result] call_id: "):
            pending.discard(content.split("call_id: ", 1)[1].split(",", 1)[0])
    return pending


def fake_script(task: dict) -> list:
    """The task's parallel calls (plus FAKE_SLOW_CALL), then per request:
    FAKE_WAITING (no calls: the model ends its turn to wait) while a call is
    still [pending], else the answer. Sync runs use two entries."""
    from tests.fakes.fake_llm import text, tool_calls
    calls = [(f"call_{task['id']}_{i}", name, args)
             for i, (name, args) in enumerate([*task["fake"]["calls"], FAKE_SLOW_CALL])]
    usage = {"prompt_tokens": 300, "completion_tokens": 20 * len(calls), "total_tokens": 300 + 20 * len(calls)}

    def after(request: dict) -> dict:
        if still_pending(request.get("messages") or []):
            return text(FAKE_WAITING, usage={"prompt_tokens": 350, "completion_tokens": 8,
                                             "total_tokens": 358})
        return text(task["fake"]["answer"], usage={"prompt_tokens": 400, "completion_tokens": 25,
                                                   "total_tokens": 425})
    return [tool_calls(calls, usage=usage), *([after] * (len(calls) + 1))]


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ForwardingLLM:
    """:5010 on a Unix socket, forwarding every POST to an OpenAI-compatible
    server (the role of the host's LLM proxy). Answers 502 when the upstream
    cannot be reached, so the guest provider's retry path applies."""

    def __init__(self, directory: str, url: str, api_key: str | None = None,
                 timeout_s: float = 300.0):
        self.path = os.path.join(directory, "v.sock_5010")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._server = None

    def start(self) -> "ForwardingLLM":
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def address_string(self):
                return "unix"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                headers = {"Content-Type": "application/json"}
                if outer.api_key:
                    headers["Authorization"] = f"Bearer {outer.api_key}"
                req = urllib.request.Request(outer.url + self.path, data=body, headers=headers,
                                             method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=outer.timeout_s) as resp:
                        status, data = resp.status, resp.read()
                        ctype = resp.headers.get("Content-Type", "application/json")
                except urllib.error.HTTPError as exc:
                    status, data = exc.code, exc.read()
                    ctype = exc.headers.get("Content-Type", "application/json")
                except OSError as exc:
                    status, ctype = 502, "application/json"
                    data = json.dumps({"error": f"upstream unreachable: {exc}"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        if os.path.exists(self.path):
            os.unlink(self.path)
        self._server = _UnixHTTPServer(self.path, Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         name="forward-llm", daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ---------------------------------------------------------------- one run
def hello_body(model: str, async_tools: bool, limits: dict) -> dict:
    return {
        "config": {
            "model": model,
            "params": {"temperature": 0, "stream": False},
            "tools": [],
            "limits": limits,
            "hooks": {},
            "async_tools": async_tools,
            "question_timeout_s": 30,
            "ui_attached": True,
            "ports": {"control": 5000, "bulk": 5001, "llm": 5010},
        },
        "session_id": SID,
        "principal": {"user": USER, "tenant": TENANT},
        "system_block": {"base_prompt": SYSTEM_PROMPT, "profile": "", "memory_index": ""},
        "resume": False,
        "at": int(time.time() * 1000),
    }


def default_limits(wall_clock_s: int, grace_s: float = DEFAULT_GRACE_S) -> dict:
    return {"wall_clock_s": wall_clock_s, "max_tools": 16, "max_turns": 8, "max_tokens": 1024,
            "op_timeout_s": {"guest": 30, "device": 5, "host": 5}, "grace_s": grace_s,
            "idle_wake_s": 600}


def metrics(events: list, task: dict, wall_s: float, fake: bool = False) -> dict:
    """The per-run row, from the delivered log."""
    from protocol.fold import replay
    state, errors = replay(events)
    settle = next((e["body"] for e in reversed(events) if e["type"] == "settle"), {})
    calls = [e["body"] for e in events if e["type"] == "tool_call"]
    per_turn: dict = {}
    for c in calls:
        per_turn[c["turn_id"]] = per_turn.get(c["turn_id"], 0) + 1
    results = [e["body"] for e in events if e["type"] == "tool_result"]
    tool_errors = sum(1 for r in results if str(r.get("content", "")).lower().startswith("error"))
    passed, missing = check(task["check"], settle.get("answer"))
    if fake:
        # the scripted answer always passes: check the tools really produced the
        # values instead (seed, tools and task agree) and that none failed
        seen, _ = check(task["check"], "\n".join(str(r.get("content", "")) for r in results))
        passed = passed and seen and tool_errors == 0
    outcome = settle.get("outcome")
    return {
        "task": task["id"],
        "pass": bool(passed and outcome == "done" and not errors),
        "missing": missing,
        "outcome": outcome,
        "turns": state.limits_consumed.get("turns", 0),
        "prompt_tokens": state.usage.get("prompt_tokens", 0),
        "completion_tokens": state.usage.get("completion_tokens", 0),
        "tool_calls": len(calls),
        "max_parallel": max(per_turn.values(), default=0),
        "tool_errors": tool_errors,
        "placeholders": len(state.placeholders),
        "late_results": state.counters.get("placeholder_fills", 0),
        "wall_s": round(wall_s, 3),
        "replay_errors": errors,
        "answer": settle.get("answer"),
    }


def run_one(task: dict, spec: dict, *, async_tools: bool, model: str, llm_url: str | None,
            api_key: str | None, work: Path, wall_clock_s: int, keep: bool = False,
            grace_s: float | None = None) -> dict:
    """One task through a fresh guest; returns the metrics row."""
    from tests.fakes.fake_host import FakeHost
    from tests.fakes.fake_llm import FakeLLM

    base = Path(tempfile.mkdtemp(prefix="ab-", dir=work))
    sock, log_dir, workspace = base / "s", base / "log", base / "ws"
    for d in (sock, log_dir, workspace):
        d.mkdir()
    seed_workspace(workspace, spec["workspace"])
    llm = (FakeLLM(str(sock), fake_script(task)) if llm_url is None
           else ForwardingLLM(str(sock), llm_url, api_key))
    if grace_s is None:
        grace_s = FAKE_GRACE_S if llm_url is None else DEFAULT_GRACE_S
    host = FakeHost(str(sock), hello_body(model, async_tools, default_limits(wall_clock_s, grace_s)))
    proc = None
    try:
        llm.start()
        host.start()
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [sys.executable, "-m", "guest.main", "--connect", f"unix:{sock}",
             "--log-dir", str(log_dir), "--workspace", str(workspace),
             "--hello-timeout", "15", "--image-version", "bench"],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        host.wait_connected(timeout=15)
        host.wait_acked_cseq(1, timeout=15)
        t0 = time.monotonic()
        host.send("task", {"input_id": f"in-{task['id']}", "text": task["prompt"],
                           "attachments": []})
        try:
            host.wait_for("settle", timeout=wall_clock_s + 30)
        except TimeoutError:
            pass
        wall = time.monotonic() - t0
        row = metrics(list(host.events), task, wall, fake=llm_url is None)
        row["async_tools"] = async_tools
        if proc.poll() is None and host.connected:
            host.send("abort", {"reason": "bench over"})
        try:
            _, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        if row["outcome"] is None:
            row["stderr_tail"] = err.decode("utf-8", "replace")[-2000:]
        return row
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        host.stop()
        llm.stop()
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- the bench
def totals(rows: list) -> dict:
    out = {}
    for flag in (False, True):
        sel = [r for r in rows if r["async_tools"] is flag]
        if not sel:
            continue
        out["on" if flag else "off"] = {
            "runs": len(sel),
            "passed": sum(r["pass"] for r in sel),
            "turns": sum(r["turns"] for r in sel),
            "prompt_tokens": sum(r["prompt_tokens"] for r in sel),
            "completion_tokens": sum(r["completion_tokens"] for r in sel),
            "tool_calls": sum(r["tool_calls"] for r in sel),
            "placeholders": sum(r["placeholders"] for r in sel),
            "late_results": sum(r["late_results"] for r in sel),
            "wall_s": round(sum(r["wall_s"] for r in sel), 3),
        }
    return out


def run_bench(spec: dict, *, fake: bool, model: str, llm_url: str | None = None,
              api_key: str | None = None, runs: int = 1, only=None, wall_clock_s: int = 180,
              work_dir=None, keep: bool = False, progress=None, grace_s: float | None = None) -> dict:
    tasks = [t for t in spec["tasks"] if not only or t["id"] in only]
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="bench-"))
    work.mkdir(parents=True, exist_ok=True)
    if keep:
        print(f"run directories kept under {work}", file=sys.stderr)
    rows = []
    try:
        for n in range(runs):
            for task in tasks:
                for flag in (False, True):
                    row = run_one(task, spec, async_tools=flag, model=model,
                                  llm_url=None if fake else llm_url, api_key=api_key,
                                  work=work, wall_clock_s=wall_clock_s, keep=keep,
                                  grace_s=grace_s)
                    row["run"] = n + 1
                    rows.append(row)
                    if progress:
                        progress(row)
    finally:
        if not work_dir and not keep:
            shutil.rmtree(work, ignore_errors=True)
    return {"model": model, "llm": "fake" if fake else llm_url, "runs": runs,
            "grace_s": grace_s if grace_s is not None else FAKE_GRACE_S if fake else DEFAULT_GRACE_S,
            "rows": rows, "totals": totals(rows)}


COLUMNS = ("task", "async", "pass", "turns", "prompt", "compl", "calls", "par", "ph", "wall_s")


def format_table(result: dict) -> str:
    lines = [" ".join(f"{c:>16}" if i == 0 else f"{c:>7}" for i, c in enumerate(COLUMNS))]
    for r in result["rows"]:
        vals = (r["task"], "on" if r["async_tools"] else "off", "PASS" if r["pass"] else "FAIL",
                r["turns"], r["prompt_tokens"], r["completion_tokens"], r["tool_calls"],
                r["max_parallel"], r["placeholders"], f"{r['wall_s']:.2f}")
        lines.append(" ".join(f"{str(v):>16}" if i == 0 else f"{str(v):>7}"
                              for i, v in enumerate(vals)))
    lines.append("")
    for mode, t in result["totals"].items():
        lines.append(f"total async={mode}: {t['passed']}/{t['runs']} passed, turns {t['turns']}, "
                     f"tokens {t['prompt_tokens']}+{t['completion_tokens']}, "
                     f"tool calls {t['tool_calls']}, placeholders {t['placeholders']}, "
                     f"wall {t['wall_s']:.2f}s")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.async_tools",
                                 description="async_tools off/on over a fixed task set")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fake", action="store_true", help="scripted LLM (CI)")
    src.add_argument("--llm-url", help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000")
    ap.add_argument("--model", default="fake-model", help="model name sent to the server")
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY"))
    ap.add_argument("--tasks", default=str(DEFAULT_TASKS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--wall-clock-s", type=int, default=180, help="per-task limit in the guest")
    ap.add_argument("--grace-s", type=float, default=None,
                    help=f"limits.grace_s (default {DEFAULT_GRACE_S}, {FAKE_GRACE_S} with --fake)")
    ap.add_argument("--work-dir", help="keep run dirs here (default: a temp dir, removed)")
    ap.add_argument("--keep", action="store_true", help="keep each run's dir (logs, workspace)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--out", help="also write the JSON result to this file")
    args = ap.parse_args(argv)
    if not args.fake and args.model == "fake-model":
        ap.error("--model is required with --llm-url")
    spec = load_tasks(args.tasks)
    only = set(args.only.split(",")) if args.only else None

    def progress(row):
        print(f"[{row['run']}] {row['task']} async={'on' if row['async_tools'] else 'off'}: "
              f"{'PASS' if row['pass'] else 'FAIL'} ({row['wall_s']:.1f}s)", file=sys.stderr)

    result = run_bench(spec, fake=args.fake, model=args.model, llm_url=args.llm_url,
                       api_key=args.api_key, runs=args.runs, only=only,
                       wall_clock_s=args.wall_clock_s, work_dir=args.work_dir, keep=args.keep,
                       progress=progress, grace_s=args.grace_s)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2) if args.json else format_table(result))
    return 0 if all(r["pass"] for r in result["rows"]) else 1


if __name__ == "__main__":
    sys.exit(main())
