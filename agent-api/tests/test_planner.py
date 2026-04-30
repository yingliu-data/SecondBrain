"""Phase 2 — Planner unit + integration tests.

Covers:
- ``should_plan`` gate logic (single-tool, multi-intent markers, length).
- ``make_plan`` happy path, bad JSON, LLM failure → all return runnable Plans.
- ``_coerce_steps`` — invalid tool names dropped, dangling deps pruned, 4-step cap.
- ``Plan.as_prompt_block`` / ``ready_steps`` / ``all_terminal``.
- End-to-end ``run_agent_loop`` with planner gated off, gated on, and failing —
  in all three cases the loop must remain functional. When the planner LLM
  raises, ``make_plan`` returns a single-step fallback Plan, so a ``plan``
  SSE event IS emitted (this matches the design doc on planner.py).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.loop import run_agent_loop
from app.agent.plan import Plan, Step
from app.agent.planner import (
    _coerce_steps,
    _fallback_plan,
    _parse_plan_json,
    make_plan,
    should_plan,
)
from app.session.session_dir import SessionDir
from app.skills.registry import ToolSet


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeLLM:
    """Records calls and returns scripted responses in order.

    Each entry in ``responses`` is either a dict (returned verbatim) or an
    Exception (raised). Once exhausted, the last response is reused.
    """

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


@dataclass
class FakeRegistry:
    tool_defs: list[dict] = field(default_factory=list)
    server_tools: set[str] = field(default_factory=set)
    device_tools: set[str] = field(default_factory=set)

    def build_tool_registry_for_session(self, session, message, *, scenario):
        return ToolSet(
            tool_defs=self.tool_defs,
            server_tool_names=self.server_tools,
            device_tool_names=self.device_tools,
            origin="test",
        )

    async def execute_server_tool(self, tool_name, arguments, *, session=None, user_id=None):
        return f"OK:{tool_name}"


def _final_text(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _two_tool_defs() -> list[dict]:
    """Two distinct tools so the planner gate doesn't short-circuit."""
    return [
        {"type": "function", "function": {"name": "web_search", "description": "search the web", "parameters": {}}},
        {"type": "function", "function": {"name": "calendar_create", "description": "create event", "parameters": {}}},
    ]


def _make_session(tmp: Path, *, user_id="u1", session_id="s1") -> SessionDir:
    return SessionDir.create(tmp / f"{user_id}_{session_id}", user_id=user_id, session_id=session_id)


# ── Unit: should_plan ────────────────────────────────────────────────────


class TestShouldPlan(unittest.IsolatedAsyncioTestCase):
    async def test_no_tools_skip(self):
        self.assertFalse(await should_plan("anything", []))

    async def test_single_tool_skip(self):
        self.assertFalse(await should_plan("anything", _two_tool_defs()[:1]))

    async def test_short_simple_skip(self):
        self.assertFalse(await should_plan("what time is it", _two_tool_defs()))

    async def test_and_marker_triggers(self):
        self.assertTrue(await should_plan("search news and add a calendar event", _two_tool_defs()))

    async def test_then_marker_triggers(self):
        self.assertTrue(await should_plan("search news then summarise", _two_tool_defs()))

    async def test_semicolon_marker_triggers(self):
        self.assertTrue(await should_plan("do x; do y", _two_tool_defs()))

    async def test_long_message_triggers(self):
        msg = "x " * 200
        self.assertTrue(await should_plan(msg, _two_tool_defs()))


# ── Unit: parsing + coercion ─────────────────────────────────────────────


class TestParsePlanJSON(unittest.TestCase):
    def test_clean_json(self):
        d = _parse_plan_json('{"steps": []}')
        self.assertEqual(d, {"steps": []})

    def test_code_fenced_json(self):
        d = _parse_plan_json('```json\n{"steps": []}\n```')
        self.assertEqual(d, {"steps": []})

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_plan_json("not json"))

    def test_array_top_level_returns_none(self):
        self.assertIsNone(_parse_plan_json("[1,2,3]"))


class TestCoerceSteps(unittest.TestCase):
    def setUp(self):
        self.tools = _two_tool_defs()

    def test_drops_invalid_tool_names(self):
        data = {"steps": [
            {"id": "s1", "goal": "do it", "mode": "inline",
             "suggested_tools": ["web_search", "made_up_tool"]},
        ]}
        steps = _coerce_steps(data, self.tools)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].suggested_tools, ["web_search"])

    def test_prunes_dangling_deps(self):
        data = {"steps": [
            {"id": "s1", "goal": "first", "mode": "inline"},
            {"id": "s2", "goal": "second", "mode": "inline",
             "depends_on": ["s1", "s99"]},  # s99 doesn't exist
        ]}
        steps = _coerce_steps(data, self.tools)
        self.assertEqual(steps[1].depends_on, ["s1"])

    def test_prunes_self_dep(self):
        data = {"steps": [
            {"id": "s1", "goal": "first", "mode": "inline", "depends_on": ["s1"]},
        ]}
        steps = _coerce_steps(data, self.tools)
        self.assertEqual(steps[0].depends_on, [])

    def test_caps_at_four_steps(self):
        data = {"steps": [
            {"id": f"s{i}", "goal": f"g{i}", "mode": "inline"}
            for i in range(1, 10)
        ]}
        steps = _coerce_steps(data, self.tools)
        self.assertEqual(len(steps), 4)

    def test_skips_empty_goal(self):
        data = {"steps": [
            {"id": "s1", "goal": "", "mode": "inline"},
            {"id": "s2", "goal": "real", "mode": "inline"},
        ]}
        steps = _coerce_steps(data, self.tools)
        self.assertEqual([s.id for s in steps], ["s2"])

    def test_invalid_mode_defaults_inline(self):
        data = {"steps": [{"id": "s1", "goal": "g", "mode": "wat"}]}
        steps = _coerce_steps(data, self.tools)
        self.assertEqual(steps[0].mode, "inline")


# ── Unit: make_plan + Plan dataclass ─────────────────────────────────────


class TestMakePlan(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_writes_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            plan_json = json.dumps({"steps": [
                {"id": "s1", "goal": "search", "mode": "inline", "suggested_tools": ["web_search"]},
                {"id": "s2", "goal": "summarise", "mode": "inline", "depends_on": ["s1"]},
            ]})
            llm = FakeLLM([_final_text(plan_json)])
            plan = await make_plan(llm, "search and summarise", _two_tool_defs(), session)

            self.assertEqual(len(plan.steps), 2)
            self.assertEqual(plan.steps[0].suggested_tools, ["web_search"])
            self.assertEqual(plan.steps[1].depends_on, ["s1"])
            self.assertTrue(plan.path.exists(), "plan artifact must be on disk")
            self.assertEqual(llm.calls[0]["tools"], None,
                             "planner must call LLM with tools=None")

    async def test_bad_json_returns_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            llm = FakeLLM([_final_text("this is not json at all")])
            plan = await make_plan(llm, "do stuff", _two_tool_defs(), session)
            self.assertEqual(len(plan.steps), 1, "fallback is single-step")
            self.assertTrue(plan.path.exists())

    async def test_empty_steps_returns_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            llm = FakeLLM([_final_text(json.dumps({"steps": []}))])
            plan = await make_plan(llm, "do stuff", _two_tool_defs(), session)
            self.assertEqual(len(plan.steps), 1)

    async def test_llm_exception_returns_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            llm = FakeLLM([RuntimeError("planner blew up")])
            plan = await make_plan(llm, "do stuff", _two_tool_defs(), session)
            self.assertEqual(len(plan.steps), 1)
            self.assertTrue(plan.path.exists())


class TestPlanArtifact(unittest.TestCase):
    def _plan(self, tmp: Path) -> Plan:
        return Plan(
            plan_id="plan_test",
            steps=[
                Step(id="s1", goal="first", mode="inline"),
                Step(id="s2", goal="second", mode="worker", depends_on=["s1"]),
                Step(id="s3", goal="third", mode="inline", depends_on=["s1"]),
            ],
            path=tmp / "plans" / "plan_test.json",
        )

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._plan(Path(td))
            p.save()
            data = json.loads(p.path.read_text())
            self.assertEqual(data["plan_id"], "plan_test")
            self.assertEqual(len(data["steps"]), 3)

    def test_ready_steps_initially(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._plan(Path(td))
            ready = p.ready_steps()
            self.assertEqual([s.id for s in ready], ["s1"])

    def test_ready_steps_after_s1_done(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._plan(Path(td))
            p.steps[0].state = "done"
            ready = p.ready_steps()
            self.assertEqual({s.id for s in ready}, {"s2", "s3"})

    def test_all_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._plan(Path(td))
            self.assertFalse(p.all_terminal())
            for s in p.steps:
                s.state = "done"
            self.assertTrue(p.all_terminal())

    def test_prompt_block(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._plan(Path(td))
            block = p.as_prompt_block()
            self.assertIn("PLAN", block)
            self.assertIn("[s1] first", block)
            self.assertIn("[s2] second", block)
            self.assertIn("← s1", block)


# ── Integration: run_agent_loop + planner ────────────────────────────────


async def _drain(gen) -> list[str]:
    out = []
    async for ev in gen:
        out.append(ev)
    return out


class TestLoopWithPlanner(unittest.IsolatedAsyncioTestCase):
    async def test_simple_query_no_plan_event(self):
        """Single-tool registry → gate is OFF, no plan event, no planner LLM call."""
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(tool_defs=_two_tool_defs()[:1])
            llm = FakeLLM([_final_text("hello")])
            events = await _drain(run_agent_loop("hi", session, registry, llm))

            plan_events = [e for e in events if e.startswith("event: plan")]
            self.assertEqual(plan_events, [])
            self.assertEqual(len(llm.calls), 1, "no planner LLM call when gate is off")

    async def test_complex_query_emits_plan_and_injects(self):
        """`and` marker + 2 tools → planner runs, plan is emitted AND added to system prompt."""
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(tool_defs=_two_tool_defs())
            plan_json = json.dumps({"steps": [
                {"id": "s1", "goal": "search news", "mode": "inline", "suggested_tools": ["web_search"]},
                {"id": "s2", "goal": "add event", "mode": "inline", "depends_on": ["s1"], "suggested_tools": ["calendar_create"]},
            ]})
            llm = FakeLLM([
                _final_text(plan_json),       # planner call
                _final_text("done"),          # action call
            ])
            events = await _drain(run_agent_loop(
                "search news and add a calendar event", session, registry, llm
            ))

            plan_events = [e for e in events if e.startswith("event: plan")]
            self.assertEqual(len(plan_events), 1, "planner emits exactly one plan event")
            payload = json.loads(plan_events[0].split("data: ", 1)[1].rstrip("\n"))
            self.assertEqual([s["id"] for s in payload["steps"]], ["s1", "s2"])

            self.assertEqual(len(llm.calls), 2, "planner + action = two LLM calls")
            action_system = llm.calls[1]["messages"][0]["content"]
            self.assertIn("PLAN", action_system, "plan block must be injected into action system prompt")
            self.assertIn("[s1] search news", action_system)

    async def test_planner_failure_emits_fallback_plan(self):
        """Planner LLM raises → make_plan returns a single-step fallback Plan.

        Per planner.py docstring: "Always returns a runnable Plan — falls back
        to a single-step stub on any failure". The loop therefore still emits
        a plan event (for the fallback), the loop continues, and the action
        LLM call still happens.
        """
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(tool_defs=_two_tool_defs())
            llm = FakeLLM([
                RuntimeError("planner blew up"),  # planner call fails
                _final_text("answered anyway"),   # action call succeeds
            ])
            events = await _drain(run_agent_loop(
                "search news and add a calendar event", session, registry, llm
            ))

            plan_events = [e for e in events if e.startswith("event: plan")]
            self.assertEqual(len(plan_events), 1,
                             "fallback plan IS emitted — make_plan never returns None")
            payload = json.loads(plan_events[0].split("data: ", 1)[1].rstrip("\n"))
            self.assertEqual(len(payload["steps"]), 1, "fallback plan has one stub step")

            done_events = [e for e in events if e.startswith("event: done")]
            self.assertEqual(len(done_events), 1, "loop still terminates cleanly")


if __name__ == "__main__":
    unittest.main()
