"""Remember skill — writes and removes durable memories.

Reads the current session + user_id from contextvars set by the registry so
concurrent tool dispatch (Phase 0 parallel gather) can't race on a shared
singleton's state.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import USERS_ROOT
from app.skills.base import BaseSkill, get_current_session, get_current_user_id
from app.user.memory import MemoryStore

logger = logging.getLogger("skills.remember")


class RememberSkill(BaseSkill):
    name = "remember"
    display_name = "Memory"
    description = "Save durable facts that persist across conversations."
    version = "1.0.0"
    execution_side = "server"
    always_available = True

    @property
    def keywords(self) -> list[str]:
        return ["remember", "note", "save", "recall", "forget"]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "remember",
                    "description": (
                        "Save a durable fact to memory. scope='user' persists across "
                        "sessions (use for user facts, preferences, recurring context). "
                        "scope='session' is scratch for this conversation only."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": (
                                    "Short lowercase identifier used as filename. "
                                    "a-z, 0-9, _ or -, max 64 chars. E.g. 'tz_pref'."
                                ),
                            },
                            "name": {
                                "type": "string",
                                "description": "Human-readable title.",
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    "One-line hook used for future recall — be "
                                    "specific; this is how the memory is retrieved."
                                ),
                            },
                            "type": {
                                "type": "string",
                                "enum": ["user", "feedback", "project", "reference"],
                                "description": (
                                    "user=facts about the person; "
                                    "feedback=how they want work done; "
                                    "project=ongoing initiatives; "
                                    "reference=pointers to external systems."
                                ),
                            },
                            "body": {
                                "type": "string",
                                "description": "The memory content (markdown allowed).",
                            },
                            "scope": {
                                "type": "string",
                                "enum": ["user", "session"],
                                "default": "user",
                            },
                        },
                        "required": ["slug", "name", "description", "type", "body"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forget",
                    "description": "Remove a memory by slug.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "slug": {"type": "string"},
                            "scope": {
                                "type": "string",
                                "enum": ["user", "session"],
                                "default": "user",
                            },
                        },
                        "required": ["slug"],
                    },
                },
            },
        ]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "remember":
            return self._handle_remember(arguments)
        if tool_name == "forget":
            return self._handle_forget(arguments)
        return f"Error: remember skill has no tool '{tool_name}'."

    def _store_for(self, scope: str) -> MemoryStore:
        if scope == "session":
            session = get_current_session()
            if session is None:
                raise RuntimeError("scope='session' requires an active session context")
            return MemoryStore(session.memory)
        user_id = get_current_user_id()
        if not user_id:
            raise RuntimeError("scope='user' requires a user context")
        return MemoryStore(Path(USERS_ROOT) / user_id / "memory")

    def _handle_remember(self, args: dict) -> str:
        scope = args.get("scope", "user")
        try:
            store = self._store_for(scope)
        except RuntimeError as e:
            return f"Error: {e}"
        try:
            rec = store.write(
                slug=args["slug"],
                name=args["name"],
                description=args["description"],
                type=args["type"],
                body=args["body"],
            )
        except KeyError as e:
            return f"Error: missing required field {e}"
        except ValueError as e:
            return f"Error: {e}"
        logger.info(f"remember scope={scope} slug={rec.slug} type={rec.type}")
        return json.dumps(
            {"status": "ok", "scope": scope, "slug": rec.slug, "type": rec.type}
        )

    def _handle_forget(self, args: dict) -> str:
        scope = args.get("scope", "user")
        try:
            store = self._store_for(scope)
        except RuntimeError as e:
            return f"Error: {e}"
        slug = args.get("slug", "")
        if not slug:
            return "Error: missing required field 'slug'"
        ok = store.remove(slug)
        logger.info(f"forget scope={scope} slug={slug} ok={ok}")
        return json.dumps(
            {"status": "ok" if ok else "not_found", "scope": scope, "slug": slug}
        )
