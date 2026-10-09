"""bench.async_tools smoke (docs/ASYNC-TOOLS.md): the fixed task set through
the real guest with the scripted LLM, async_tools off and on; the
--llm-url path through a local OpenAI-compatible double."""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from bench import async_tools as bench
from tests.fakes.fake_llm import expand


def test_task_set_is_well_formed():
    spec = bench.load_tasks()
    assert len(spec["tasks"]) >= 8
    for task in spec["tasks"]:
        assert len(task["fake"]["calls"]) >= 2, f"{task['id']}: needs parallel calls"
        assert {name for name, _ in task["fake"]["calls"]} <= {"bash", "read", "grep"}
        assert bench.check(task["check"], task["fake"]["answer"])[0], task["id"]


def test_checker_matches_whole_tokens_case_insensitively():
    spec = {"contains_all": ["7", "Priya Raman", "2.4.1"]}
    assert bench.check(spec, "7 rows; owner priya raman; v2.4.1")[0] is False   # v2.4.1 glued
    assert bench.check(spec, "7 rows; owner priya raman; version 2.4.1.")[0]
    assert bench.check(spec, "17 rows, Priya Raman, 2.4.1") == (False, ["7"])


def test_fake_bench_runs_every_task_off_and_on():
    spec = bench.load_tasks()
    result = bench.run_bench(spec, fake=True, model="fake-model")
    rows = result["rows"]
    assert len(rows) == 2 * len(spec["tasks"])
    bad = [(r["task"], r["async_tools"], r["missing"], r["outcome"], r["replay_errors"])
           for r in rows if not r["pass"]]
    assert not bad
    for r in rows:
        assert r["turns"] == 2 and r["tool_calls"] == r["max_parallel"] >= 2
        assert r["prompt_tokens"] > 0 and r["tool_errors"] == 0
    assert set(result["totals"]) == {"off", "on"}
    assert "total async=on" in bench.format_table(result)


class _OpenAIDouble(http.server.BaseHTTPRequestHandler):
    """A TCP chat-completions server serving a script (the vLLM stand-in)."""
    protocol_version = "HTTP/1.1"
    script: list = []
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append((self.path, body))
        data = json.dumps(expand(self.script.pop(0))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def openai_double():
    task = bench.load_tasks()["tasks"][0]
    handler = type("H", (_OpenAIDouble,), {"script": bench.fake_script(task), "seen": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", handler, task
    server.shutdown()
    server.server_close()


def test_llm_url_mode_forwards_to_the_server(openai_double, tmp_path):
    url, handler, task = openai_double
    spec = bench.load_tasks()
    row = bench.run_one(task, spec, async_tools=False, model="Qwen/Qwen3-14B", llm_url=url,
                        api_key=None, work=tmp_path, wall_clock_s=30)
    assert row["pass"] and row["turns"] == 2
    assert [p for p, _ in handler.seen] == ["/v1/chat/completions"] * 2
    assert all(b["model"] == "Qwen/Qwen3-14B" and b["stream"] is False for _, b in handler.seen)
