"""Host tools over ast-everything (`ast_everything.tools`): Python and Markdown files as
AST-style nodes and edges in MongoDB, and the search over them.

This skill gives the tools the session's workspace (`workspace_path`: the host copy,
synced when a task ends) and hides the other users' memory sources. The MongoDB
credentials are agent-api's (`ast_everything.store.settings`: the environment, else the
repo `.env`); the guest never sees them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ast_everything.store import Store
from ast_everything.tools import AstTools

from app.config import AST_REPOS_DIR
from app.host.memory_index import memory_source
from app.skills.base import BaseSkill, get_current_session, get_current_user_id


def _workspace() -> str | None:
    session = get_current_session()
    return session.workspace if session is not None else None


def _other_users_memory(store: Store) -> list[str]:
    """The memory sources (each has session records) of every user but the current one."""
    own = memory_source(get_current_user_id())
    return [source for source in store.sessions.distinct("source") if source != own]


class AstIndexSkill(BaseSkill):
    name = "ast_index"
    display_name = "AST Index"
    description = "Index projects and documents (Python, Markdown) into MongoDB and search them."
    version = "1.0.0"
    execution_side = "server"
    keywords = ["index", "ast", "parse", "mongodb", "codebase", "markdown", "search code"]

    def __init__(self, connect=Store.connect, repos_dir: str | Path = AST_REPOS_DIR):
        self.tools = AstTools(connect, repos_dir, workspace=_workspace, hidden=_other_users_memory)

    def get_tool_definitions(self) -> list[dict]:
        return self.tools.definitions

    async def execute(self, tool_name: str, arguments: dict) -> str:
        return await asyncio.to_thread(self.tools.call, tool_name, arguments)
