"""The agent tools: their three index inputs, the background job, the read tools and the
agent's hooks, on the fake store (no Atlas)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from ast_everything import store as store_module
from ast_everything import tools as tools_module
from ast_everything.tools import AstTools


@pytest.fixture
def tools(store, tmp_path):
    return AstTools(lambda: store, tmp_path / "repos")


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "app.py").write_text("def main():\n    return helper()\n\n\ndef helper():\n    return 1\n")
    (root / "docs" / "guide.md").write_text("# Guide\n\nRun [main](../app.py).\n\n## Setup\n\n- uv sync\n")
    return root


def call(tools, tool, **args):
    return tools.call(tool, args)


def finished(tools, job_id, timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = json.loads(call(tools, "ast_index_status", job_id=job_id))
        if status["state"] != "running":
            return status
        assert time.monotonic() < deadline, "the index job did not finish"
        time.sleep(0.02)


def index(tools, **args) -> dict:
    started = json.loads(call(tools, "ast_index", **args))
    assert "job_id" in started, started
    return finished(tools, started["job_id"])


def test_the_definitions_name_every_tool():
    assert [d["function"]["name"] for d in AstTools.definitions] == [
        "ast_index", "ast_index_status", "ast_sources", "ast_search", "ast_traverse", "ast_retrieve",
        "ast_search_by_time"]


def test_index_a_host_folder_then_search_traverse_and_retrieve(tools, project):
    status = index(tools, path=str(project))
    assert (status["state"], status["source"]) == ("done", "proj")
    assert status["counts"]["files"] == 2 and status["counts"]["skipped"] == 0
    assert json.loads(call(tools, "ast_sources")) == {"sources": [{"source": "proj", "files": 2, "skipped": 0}]}
    found = json.loads(call(tools, "ast_search", query="setup", scope="proj"))
    assert found["results"][0]["id"] == "proj/docs/guide.md#guide/setup@v1"
    walked = json.loads(call(tools, "ast_traverse", start_ids=["proj/docs/guide.md#guide:p1"], edge_types=["links_to"]))
    assert {"type": "links_to", "src": "proj/docs/guide.md#guide:p1", "dst": "proj/app.py"} in walked["edges"]
    [main] = json.loads(call(tools, "ast_retrieve", ids=["proj/app.py:main"]))["results"]
    assert (main["lines"], main["text"]) == ([1, 2], "def main():\n    return helper()")
    assert json.loads(call(tools, "ast_search", query="nothing-like-it"))["message"] == "no layer matched"


def test_a_hidden_source_is_not_listed_searched_or_read(store, tmp_path, project):
    index(AstTools(lambda: store, tmp_path), path=str(project))
    tools = AstTools(lambda: store, tmp_path, hidden=lambda store: ["proj"])
    assert json.loads(call(tools, "ast_sources")) == {"sources": []}
    assert json.loads(call(tools, "ast_search", query="proj/app.py:main"))["results"] == []
    assert "cannot be read" in call(tools, "ast_search", query="main", scope="proj")
    assert "cannot be read" in call(tools, "ast_retrieve", ids=["proj/app.py:main"])
    assert "cannot be read" in call(tools, "ast_traverse", start_ids=["proj/app.py"])


def test_index_one_file_under_a_given_name(tools, project):
    status = index(tools, path=str(project / "docs" / "guide.md"), name="handbook")
    assert (status["state"], status["source"], status["counts"]["files"]) == ("done", "handbook", 1)
    assert json.loads(call(tools, "ast_search", query="handbook/guide.md#guide"))["layer"] == 1


def test_index_the_workspace(store, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("# Notes\n")
    tools = AstTools(lambda: store, tmp_path / "repos", workspace=lambda: workspace)
    status = index(tools, workspace_path="/workspace/notes.md")
    assert (status["state"], status["source"]) == ("done", "notes")
    assert call(tools, "ast_index", workspace_path="../../proj").startswith("Error: workspace_path is outside")


def test_workspace_path_needs_a_workspace(tools):
    assert call(tools, "ast_index", workspace_path="notes.md") == "Error: workspace_path needs a session"


def test_index_a_git_repo_clone_then_fetch(tools, project, monkeypatch, tmp_path):
    monkeypatch.setattr(tools_module, "GIT_SCHEMES", (*tools_module.GIT_SCHEMES, "file"))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run([*git, "-C", str(project), "add", "."], check=True)
    subprocess.run([*git, "-C", str(project), "commit", "-qm", "one"], check=True)
    url = f"file://{project}"
    first = index(tools, git_url=url)
    assert (first["state"], first["source"], first["counts"]["files"]) == ("done", "proj", 2)
    (project / "more.md").write_text("# More\n")
    subprocess.run([*git, "-C", str(project), "add", "."], check=True)
    subprocess.run([*git, "-C", str(project), "commit", "-qm", "two"], check=True)
    second = index(tools, git_url=url)
    assert second["counts"]["files"] == 3 and second["counts"]["unchanged"] == 2
    assert len(list((tmp_path / "repos").iterdir())) == 1


def test_a_failed_job_reports_its_error(tools, monkeypatch):
    monkeypatch.setattr(tools_module, "GIT_SCHEMES", (*tools_module.GIT_SCHEMES, "file"))
    status = index(tools, git_url="file:///no/such/repo")
    assert status["state"] == "failed" and status["error"].startswith("git clone failed")


@pytest.mark.parametrize("args, message", [
    ({}, "exactly one of"),
    ({"path": "/a", "git_url": "https://x/y"}, "exactly one of"),
    ({"path": "relative/dir"}, "must be absolute"),
    ({"path": "/no/such/folder"}, "no such file"),
    ({"git_url": "ftp://host/repo"}, "https or ssh"),
    ({"path": "/tmp", "name": "a/b"}, "source name"),
    ({"path": "/tmp", "name": "memory-alice"}, "memory- prefix"),
])
def test_bad_index_input_is_refused(tools, args, message):
    answer = call(tools, "ast_index", **args)
    assert answer.startswith("Error:") and message in answer


def test_a_file_no_parser_reads_is_refused(tools, tmp_path):
    (tmp_path / "x.txt").write_text("text")
    assert "no parser for x.txt" in call(tools, "ast_index", path=str(tmp_path / "x.txt"))


@pytest.mark.parametrize("tool, args, message", [
    ("ast_search", {"query": "x", "limit": 0}, "limit must be from 1 to 100"),
    ("ast_search", {"query": " "}, "query is required"),
    ("ast_traverse", {"start_ids": ["x"], "hops": 9}, "hops must be from 1 to 5"),
    ("ast_traverse", {"start_ids": ["missing"]}, "unknown IDs"),
    ("ast_retrieve", {"ids": ["x"], "detail": "all"}, "detail must be one of"),
    ("ast_search_by_time", {"time_range": ["2026-01-01"]}, "time_range must be"),
    ("ast_index_status", {"job_id": "nope"}, "unknown job_id"),
    ("search", {}, "no tool 'search'"),
])
def test_bad_read_arguments_name_the_argument(tools, tool, args, message):
    answer = call(tools, tool, **args)
    assert answer.startswith("Error:") and message in answer


def test_missing_credentials_name_the_variable(monkeypatch, tmp_path):
    """Without MONGODB_* (and no .env), a tool answers with the variable to set."""
    monkeypatch.setattr(store_module, "find_dotenv", lambda *a, **k: "")
    for name in ("MONGODB_USERNAME", "MONGODB_PASSWORD", "MONGODB_HOST"):
        monkeypatch.delenv(name, raising=False)
    answer = call(AstTools(repos_dir=tmp_path), "ast_sources")
    assert answer.startswith("Error: set MONGODB_USERNAME, MONGODB_PASSWORD, MONGODB_HOST")
