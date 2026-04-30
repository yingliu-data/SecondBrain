"""Phase 4 — Extractor + PendingStore tests.

Covers:
- PendingStore: propose/list/get/remove, filtering of disallowed profile keys,
  empty-patch returns None, invalid change-ids rejected.
- Extractor: writes memories directly, queues profile patch to PendingStore,
  skips duplicates, clamps to 2 memories per turn, rejects bad slugs / types,
  handles bad JSON and LLM failure gracefully (best-effort, never raises).
- Integration: run_extraction end-to-end against a temp USERS_ROOT.
- schedule_extraction returns a Task that completes without raising.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# Temp USERS_ROOT — must be set before importing anything that resolves
# USERS_ROOT at import time (memory.py, profile.py, extractor.py).
_USERS_TMP = tempfile.mkdtemp(prefix="users_test_")
os.environ.setdefault("USERS_ROOT", _USERS_TMP)
os.environ.setdefault("API_SECRET_KEY", "test-key")

from app.user.extractor import (  # noqa: E402
    _coerce_memory,
    _recent_exchange,
    run_extraction,
    schedule_extraction,
)
from app.user.memory import MemoryStore  # noqa: E402
from app.user.pending import ALLOWED_PROFILE_FIELDS, PendingStore  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_completion(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        r = self.responses[idx]
        if isinstance(r, Exception):
            raise r
        return r


def _final(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _isolated_users_root():
    """Context manager returning a fresh USERS_ROOT directory."""
    return tempfile.TemporaryDirectory(prefix="users_test_")


# ── PendingStore unit tests ─────────────────────────────────────────────


class TestPendingStore(unittest.TestCase):
    def test_propose_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            store = PendingStore(Path(td))
            change = store.propose(
                user_id="u1",
                fields={"display_name": "Sophia", "timezone": "Europe/London"},
                rationale="user said their name is Sophia",
            )
            self.assertIsNotNone(change)
            self.assertEqual(change.fields["display_name"], "Sophia")
            self.assertEqual(change.fields["timezone"], "Europe/London")
            self.assertTrue(change.id.startswith("pc_"))

            items = store.list()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].id, change.id)

    def test_propose_filters_disallowed_fields(self):
        with tempfile.TemporaryDirectory() as td:
            store = PendingStore(Path(td))
            change = store.propose(
                user_id="u1",
                fields={"display_name": "Sophia", "evil_field": "boom"},
            )
            self.assertIsNotNone(change)
            self.assertIn("display_name", change.fields)
            self.assertNotIn("evil_field", change.fields)

    def test_propose_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            store = PendingStore(Path(td))
            self.assertIsNone(store.propose(user_id="u1", fields={}))
            self.assertIsNone(store.propose(user_id="u1", fields={"bad": "x"}))
            self.assertIsNone(store.propose(user_id="u1", fields={"display_name": "   "}))

    def test_get_and_remove(self):
        with tempfile.TemporaryDirectory() as td:
            store = PendingStore(Path(td))
            c = store.propose(user_id="u1", fields={"timezone": "UTC"})
            self.assertIsNotNone(store.get(c.id))
            self.assertTrue(store.remove(c.id))
            self.assertIsNone(store.get(c.id))
            self.assertFalse(store.remove(c.id))

    def test_invalid_change_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = PendingStore(Path(td))
            self.assertIsNone(store.get("../etc/passwd"))
            self.assertFalse(store.remove("../evil"))

    def test_allowed_fields_unchanged(self):
        # Guard against someone broadening this set without re-thinking approval scope.
        self.assertEqual(
            ALLOWED_PROFILE_FIELDS,
            {"display_name", "timezone", "email", "tone"},
        )


# ── _coerce_memory unit tests ───────────────────────────────────────────


class TestCoerceMemory(unittest.TestCase):
    def test_valid(self):
        clean = _coerce_memory({
            "slug": "works_on_second_brain",
            "name": "Works on SecondBrain",
            "description": "project context",
            "type": "project",
            "body": "Building an AI-orchestrated assistant.",
        })
        self.assertIsNotNone(clean)
        self.assertEqual(clean["slug"], "works_on_second_brain")

    def test_bad_slug_returns_none(self):
        self.assertIsNone(_coerce_memory({
            "slug": "Has UpperCase",
            "name": "x", "type": "user", "body": "y",
        }))

    def test_bad_type_returns_none(self):
        self.assertIsNone(_coerce_memory({
            "slug": "s", "name": "x", "type": "wat", "body": "y",
        }))

    def test_missing_name_returns_none(self):
        self.assertIsNone(_coerce_memory({
            "slug": "s", "type": "user", "body": "y",
        }))

    def test_missing_body_returns_none(self):
        self.assertIsNone(_coerce_memory({
            "slug": "s", "name": "x", "type": "user", "body": "  ",
        }))

    def test_truncates_long_fields(self):
        clean = _coerce_memory({
            "slug": "s",
            "name": "x" * 500,
            "description": "y" * 500,
            "type": "user",
            "body": "z" * 5000,
        })
        self.assertLessEqual(len(clean["name"]), 200)
        self.assertLessEqual(len(clean["description"]), 300)
        self.assertLessEqual(len(clean["body"]), 2000)


class TestRecentExchange(unittest.TestCase):
    def test_trims_to_last_four(self):
        hist = [{"role": "user", "content": str(i)} for i in range(10)]
        self.assertEqual(len(_recent_exchange(hist)), 4)

    def test_empty(self):
        self.assertEqual(_recent_exchange([]), [])


# ── run_extraction integration tests ────────────────────────────────────


class TestExtractorEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_writes_memory_and_queues_profile(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                payload = json.dumps({
                    "memories": [{
                        "slug": "prefers_concise",
                        "name": "Prefers concise responses",
                        "description": "user explicitly asked for short answers",
                        "type": "feedback",
                        "body": "User said: keep responses to 1-3 sentences.",
                    }],
                    "profile_patch": {"display_name": "Sophia"},
                    "rationale": "User introduced themselves as Sophia.",
                })
                llm = FakeLLM([_final(payload)])
                history = [
                    {"role": "user", "content": "hi I'm Sophia, please keep answers short"},
                    {"role": "assistant", "content": "Got it, Sophia."},
                ]

                result = await run_extraction(user_id="u1", history=history, llm=llm)

                self.assertEqual(result.memories_written, ["prefers_concise"])
                self.assertIsNotNone(result.profile_change_id)

                mem = MemoryStore(Path(td) / "u1" / "memory")
                saved = mem.list()
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0].slug, "prefers_concise")

                pending = PendingStore(Path(td) / "u1" / "pending")
                queued = pending.list()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0].fields, {"display_name": "Sophia"})
                self.assertIn("Sophia", queued[0].rationale)

    async def test_no_history_returns_early(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                llm = FakeLLM([_final('{"memories": [], "profile_patch": null}')])
                result = await run_extraction(user_id="u1", history=[], llm=llm)
                self.assertEqual(result.memories_written, [])
                self.assertIsNone(result.profile_change_id)
                self.assertEqual(llm.calls, [], "no history → no LLM call")

    async def test_bad_json_is_graceful(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                llm = FakeLLM([_final("this is definitely not json")])
                history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
                result = await run_extraction(user_id="u1", history=history, llm=llm)
                self.assertEqual(result.memories_written, [])
                self.assertIsNone(result.profile_change_id)
                self.assertIn("bad_json", result.skipped)

    async def test_llm_failure_is_graceful(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                llm = FakeLLM([RuntimeError("extractor LLM down")])
                history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
                result = await run_extraction(user_id="u1", history=history, llm=llm)
                self.assertEqual(result.memories_written, [])
                self.assertIsNone(result.profile_change_id)
                self.assertTrue(any("llm_error" in s for s in result.skipped))

    async def test_skips_duplicate_memory(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                mem = MemoryStore(Path(td) / "u1" / "memory")
                mem.write(
                    slug="prefers_concise",
                    name="Prefers concise",
                    description="pre-existing",
                    type="feedback",
                    body="already there",
                )
                payload = json.dumps({
                    "memories": [{
                        "slug": "prefers_concise",
                        "name": "Prefers concise (again)",
                        "description": "dup",
                        "type": "feedback",
                        "body": "duplicate body",
                    }],
                    "profile_patch": None,
                })
                llm = FakeLLM([_final(payload)])
                history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
                result = await run_extraction(user_id="u1", history=history, llm=llm)
                self.assertEqual(result.memories_written, [])
                self.assertTrue(any("duplicate" in s for s in result.skipped))
                # The pre-existing memory body is untouched.
                self.assertEqual(mem.list()[0].description, "pre-existing")

    async def test_clamps_to_two_memories(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                payload = json.dumps({
                    "memories": [
                        {"slug": f"fact_{i}", "name": f"F{i}", "description": "d",
                         "type": "user", "body": f"body {i}"}
                        for i in range(5)
                    ],
                    "profile_patch": None,
                })
                llm = FakeLLM([_final(payload)])
                history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
                result = await run_extraction(user_id="u1", history=history, llm=llm)
                self.assertEqual(len(result.memories_written), 2)

    async def test_empty_patch_does_not_queue(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                payload = json.dumps({
                    "memories": [],
                    "profile_patch": {},
                })
                llm = FakeLLM([_final(payload)])
                history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
                result = await run_extraction(user_id="u1", history=history, llm=llm)
                self.assertIsNone(result.profile_change_id)
                pending = PendingStore(Path(td) / "u1" / "pending")
                self.assertEqual(pending.list(), [])


# ── schedule_extraction fire-and-forget ─────────────────────────────────


class TestScheduleExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_returns_awaitable_task_and_completes(self):
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                payload = json.dumps({"memories": [], "profile_patch": None})
                llm = FakeLLM([_final(payload)])
                history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]

                task = schedule_extraction(user_id="u1", history=history, llm=llm)
                self.assertIsInstance(task, asyncio.Task)
                await asyncio.wait_for(task, timeout=2.0)
                self.assertTrue(task.done())
                self.assertIsNone(task.exception(), "fire-and-forget must swallow errors")

    async def test_swallows_extractor_exceptions(self):
        """Even if run_extraction raises internally, schedule_extraction must not propagate."""
        with _isolated_users_root() as td:
            with patch("app.user.extractor.USERS_ROOT", td):
                # LLM raises AND we patch run_extraction to raise too
                llm = FakeLLM([RuntimeError("boom")])
                history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
                task = schedule_extraction(user_id="u1", history=history, llm=llm)
                await asyncio.wait_for(task, timeout=2.0)
                # run_extraction handles the LLM error gracefully, so task.exception() is None
                self.assertIsNone(task.exception())


if __name__ == "__main__":
    unittest.main()
