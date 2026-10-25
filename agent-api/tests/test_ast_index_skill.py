"""The ast_index host skill: the ast-everything tools (tested in ast-everything/tests/test_tools.py)
with the session's workspace and the current user's memory, on mongomock (no Atlas Search: the
name and ID layers answer)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest

from ast_everything.memory import MemoryFiles, Turn, index_session
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


def test_memory_source_is_searchable(skill, mongo, project, monkeypatch):
    """Criterion 5 of issue #56 (plan step 5): an indexed chat session is found by ID, by name and
    by time, in every source by default and with `scope` set to the memory source; ast_traverse walks
    from the session to its turns and from one turn to the next."""
    monkeypatch.setattr(skill_module, "get_current_user_id", lambda: "u")
    index(skill, path=str(project))
    at = datetime(2026, 10, 24, tzinfo=timezone.utc)
    index_session(mongo, "memory-u", "sessions/t/s1", "Choosing an editor",
                  [Turn(1, "user", at, "Which editor?"), Turn(2, "assistant", at, "Try helix.")])
    session = "memory-u/sessions/t/s1"
    assert json.loads(call(skill, "ast_search", query="s1"))["results"][0]["id"] == f"{session}@v1"
    found = json.loads(call(skill, "ast_search", query=f"{session}:turn-2", scope="memory-u", detail="full"))
    assert [(r["type"], r["text"]) for r in found["results"]] == [("turn", "Try helix.")]
    assert json.loads(call(skill, "ast_search", query="main", scope="memory-u"))["results"] == []
    recent = json.loads(call(skill, "ast_search_by_time", time_range=["2026-01-01", None], node_types=["turn"]))
    assert len(recent["results"]) == 2
    walked = json.loads(call(skill, "ast_traverse", start_ids=[session], hops=2, edge_types=["contains", "precedes"]))
    assert {(e["type"], e["src"], e["dst"]) for e in walked["edges"]} == {
        ("contains", session, f"{session}:turn-1"), ("contains", session, f"{session}:turn-2"),
        ("precedes", f"{session}:turn-1", f"{session}:turn-2")}


def test_another_users_memory_is_hidden(skill, mongo, tmp_path, monkeypatch):
    """Issue #56, review round 2, request 1: another user's agent finds none of u's turns or
    memory files, unscoped, and a scope or an ID in `memory-u` is refused."""
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "editor.md").write_text("# Editor\n\nUses vim.\n")
    index_session(mongo, "memory-u", "sessions/t/s1", "Choosing an editor",
                  [Turn(1, "user", datetime(2026, 10, 24, tzinfo=timezone.utc), "Which editor?")],
                  memory=[MemoryFiles(memory, ["editor"])])
    monkeypatch.setattr(skill_module, "get_current_user_id", lambda: "bob")
    assert json.loads(call(skill, "ast_search", query="s1"))["results"] == []
    assert json.loads(call(skill, "ast_search", query="memory-u/editor.md"))["results"] == []
    assert json.loads(call(skill, "ast_search_by_time", time_range=["2026-01-01", None]))["results"] == []
    assert json.loads(call(skill, "ast_sources")) == {"sources": []}
    assert "cannot be read" in call(skill, "ast_search", query="s1", scope="memory-u")
    assert "cannot be read" in call(skill, "ast_retrieve", ids=["memory-u/editor.md"])
    assert "cannot be read" in call(skill, "ast_traverse", start_ids=["memory-u/sessions/t/s1"])


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
