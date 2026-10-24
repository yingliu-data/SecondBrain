"""The ast_index host skill: its three inputs, the background job, and the read tools,
on mongomock (no Atlas Search: the name and ID layers answer)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import mongomock
import pytest

from ast_everything import store as store_module
from ast_everything.store import Store

from app.skills import base
from app.skills.ast_index import skill as skill_module
from app.skills.ast_index.skill import AstIndexSkill
from app.skills.registry import SkillRegistry


def _accept_pymongo_bulk_kwargs():
    """mongomock 4.3 predates pymongo 4.11's `sort` on UpdateOne, which bulk_write passes on."""
    from mongomock.collection import BulkOperationBuilder
    add_update = BulkOperationBuilder.add_update

    def patched(self, selector, doc, multi=False, upsert=False, collation=None, array_filters=None, hint=None, **_):
        return add_update(self, selector, doc, multi, upsert, collation, array_filters, hint)
    BulkOperationBuilder.add_update = patched


_accept_pymongo_bulk_kwargs()


class MockStore(Store):
    """Store on mongomock, which has no Atlas Search: those layers find nothing."""

    def ensure_search_indexes(self):
        pass

    def keyword_names(self, query, match, limit=10):
        return []

    keyword_text = vector = keyword_names


@pytest.fixture
def mongo():
    return MockStore(mongomock.MongoClient(tz_aware=True), "test")


@pytest.fixture
def skill(mongo, tmp_path):
    return AstIndexSkill(connect=lambda: mongo, repos_dir=tmp_path / "repos")


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "app.py").write_text("def main():\n    return helper()\n\n\ndef helper():\n    return 1\n")
    (root / "docs" / "guide.md").write_text("# Guide\n\nRun [main](../app.py).\n\n## Setup\n\n- uv sync\n")
    return root


def call(skill, tool, **args):
    return asyncio.run(skill.execute(tool, args))


def finished(skill, job_id, timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = json.loads(call(skill, "ast_index_status", job_id=job_id))
        if status["state"] != "running":
            return status
        assert time.monotonic() < deadline, "the index job did not finish"
        time.sleep(0.02)


def index(skill, **args) -> dict:
    started = json.loads(call(skill, "ast_index", **args))
    assert "job_id" in started, started
    return finished(skill, started["job_id"])


def test_the_registry_offers_the_tools_as_host_tools():
    names = set(SkillRegistry().get_server_tool_names(None))
    assert {"ast_index", "ast_index_status", "ast_sources", "ast_search", "ast_traverse",
            "ast_retrieve", "ast_search_by_time"} <= names


def test_index_a_host_folder_then_search_traverse_and_retrieve(skill, project):
    status = index(skill, path=str(project))
    assert (status["state"], status["source"]) == ("done", "proj")
    assert status["counts"]["files"] == 2 and status["counts"]["skipped"] == 0
    assert json.loads(call(skill, "ast_sources")) == {"sources": [{"source": "proj", "files": 2, "skipped": 0}]}
    found = json.loads(call(skill, "ast_search", query="setup", scope="proj"))
    assert found["results"][0]["id"] == "proj/docs/guide.md#guide/setup@v1"
    walked = json.loads(call(skill, "ast_traverse", start_ids=["proj/docs/guide.md#guide:p1"], edge_types=["links_to"]))
    assert {"type": "links_to", "src": "proj/docs/guide.md#guide:p1", "dst": "proj/app.py"} in walked["edges"]
    [main] = json.loads(call(skill, "ast_retrieve", ids=["proj/app.py:main"]))["results"]
    assert (main["lines"], main["text"]) == ([1, 2], "def main():\n    return helper()")
    assert json.loads(call(skill, "ast_search", query="nothing-like-it"))["message"] == "no layer matched"


def test_index_one_file_under_a_given_name(skill, project):
    status = index(skill, path=str(project / "docs" / "guide.md"), name="handbook")
    assert (status["state"], status["source"], status["counts"]["files"]) == ("done", "handbook", 1)
    assert json.loads(call(skill, "ast_search", query="handbook/guide.md#guide"))["layer"] == 1


def test_index_the_session_workspace(skill, project, monkeypatch, tmp_path):
    class Session:
        workspace = tmp_path / "session" / "workspace"
    Session.workspace.mkdir(parents=True)
    (Session.workspace / "notes.md").write_text("# Notes\n")
    base.set_current_context(Session(), "user")
    try:
        status = index(skill, workspace_path="/workspace/notes.md")
        assert (status["state"], status["source"]) == ("done", "notes")
        assert call(skill, "ast_index", workspace_path="../../proj").startswith("Error: workspace_path is outside")
    finally:
        base.set_current_context(None, "")


def test_index_a_git_repo_clone_then_fetch(skill, project, monkeypatch, tmp_path):
    monkeypatch.setattr(skill_module, "GIT_SCHEMES", (*skill_module.GIT_SCHEMES, "file"))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run([*git, "-C", str(project), "add", "."], check=True)
    subprocess.run([*git, "-C", str(project), "commit", "-qm", "one"], check=True)
    url = f"file://{project}"
    first = index(skill, git_url=url)
    assert (first["state"], first["source"], first["counts"]["files"]) == ("done", "proj", 2)
    (project / "more.md").write_text("# More\n")
    subprocess.run([*git, "-C", str(project), "add", "."], check=True)
    subprocess.run([*git, "-C", str(project), "commit", "-qm", "two"], check=True)
    second = index(skill, git_url=url)
    assert second["counts"]["files"] == 3 and second["counts"]["unchanged"] == 2
    assert len(list((tmp_path / "repos").iterdir())) == 1


def test_a_failed_job_reports_its_error(skill, monkeypatch):
    monkeypatch.setattr(skill_module, "GIT_SCHEMES", (*skill_module.GIT_SCHEMES, "file"))
    status = index(skill, git_url="file:///no/such/repo")
    assert status["state"] == "failed" and status["error"].startswith("git clone failed")


@pytest.mark.parametrize("args, message", [
    ({}, "exactly one of"),
    ({"path": "/a", "git_url": "https://x/y"}, "exactly one of"),
    ({"path": "relative/dir"}, "must be absolute"),
    ({"path": "/no/such/folder"}, "no such file"),
    ({"git_url": "ftp://host/repo"}, "https or ssh"),
    ({"path": "/tmp", "name": "a/b"}, "source name"),
])
def test_bad_index_input_is_refused(skill, args, message):
    answer = call(skill, "ast_index", **args)
    assert answer.startswith("Error:") and message in answer


def test_a_file_no_parser_reads_is_refused(skill, tmp_path):
    (tmp_path / "x.txt").write_text("text")
    assert "no parser for x.txt" in call(skill, "ast_index", path=str(tmp_path / "x.txt"))


@pytest.mark.parametrize("tool, args, message", [
    ("ast_search", {"query": "x", "limit": 0}, "limit must be from 1 to 100"),
    ("ast_search", {"query": " "}, "query is required"),
    ("ast_traverse", {"start_ids": ["x"], "hops": 9}, "hops must be from 1 to 5"),
    ("ast_traverse", {"start_ids": ["missing"]}, "unknown IDs"),
    ("ast_retrieve", {"ids": ["x"], "detail": "all"}, "detail must be one of"),
    ("ast_search_by_time", {"time_range": ["2026-01-01"]}, "time_range must be"),
    ("ast_index_status", {"job_id": "nope"}, "unknown job_id"),
])
def test_bad_read_arguments_name_the_argument(skill, tool, args, message):
    answer = call(skill, tool, **args)
    assert answer.startswith("Error:") and message in answer


def test_missing_credentials_name_the_variable(monkeypatch, tmp_path):
    """Without MONGODB_* (and no .env), a tool answers with the variable to set."""
    monkeypatch.setattr(store_module, "find_dotenv", lambda *a, **k: "")
    for name in ("MONGODB_USERNAME", "MONGODB_PASSWORD", "MONGODB_HOST"):
        monkeypatch.delenv(name, raising=False)
    answer = call(AstIndexSkill(repos_dir=tmp_path), "ast_sources")
    assert answer.startswith("Error: set MONGODB_USERNAME, MONGODB_PASSWORD, MONGODB_HOST")
