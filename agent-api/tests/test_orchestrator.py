"""Phase 3 — Worker + Orchestrator tests.

Covers:
- Worker runs a single step, dispatches server tools, returns a summary.
- Worker handles LLM failure (state=failed, result_summary populated).
- Worker refuses device tools (doesn't deadlock; returns an error tool result
  so the LLM can recover).
- Orchestrator runs multi-step plans in dependency waves with bounded
  concurrency and emits ``step_start`` / ``step_done`` SSE events.
- Orchestrator calls the synthesizer exactly once and streams its tokens.
- Orchestrator falls through to the inline action loop for single-step plans
  and for the planner-gate-off path.
- Upstream step failures short-circuit dependent steps without deadlock.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.orchestrator import run_agent_turn
from app.agent.plan import Plan, Step
from app.agent.worker import run_step_as_worker
from app.session.session_dir import SessionDir
from app.skills.registry import ToolSet


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeLLM:
    """Returns scripted responses by matching the first system prompt.

    Each entry is (predicate(messages) -> bool, response_or_exception). The
    first matching entry is used and consumed (single-use unless reusable=True).
    Falls back to the final entry when nothing else matches.
    """

    def __init__(self, entries):
        self.entries = list(entries)
        self.calls: list[dict] = []

    async def chat_completion(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        for entry in self.entries:
            pred, resp = entry[0], entry[1]
            if pred(messages):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"No FakeLLM response matched. Calls so far: {len(self.calls)}")


class OrderedFakeLLM:
    """Returns scripted responses in call order (simpler when order is predictable)."""

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
    server_results: dict[str, str] = field(default_factory=dict)
    server_call_log: list[tuple[str, dict]] = field(default_factory=list)

    def build_tool_registry_for_session(self, session, message, *, scenario):
        return ToolSet(
            tool_defs=self.tool_defs,
            server_tool_names=self.server_tools,
            device_tool_names=self.device_tools,
            origin="test",
        )

    async def execute_server_tool(self, tool_name, arguments, *, session=None, user_id=None):
        self.server_call_log.append((tool_name, arguments))
        return self.server_results.get(tool_name, f"OK:{tool_name}")


def _final(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _tool_call(tc_id: str, name: str, args: dict) -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _tool_response(calls: list[dict]) -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": calls},
            "finish_reason": "tool_calls",
        }]
    }


def _make_session(tmp: Path, *, user_id="u1", session_id="s1") -> SessionDir:
    return SessionDir.create(tmp / f"{user_id}_{session_id}", user_id=user_id, session_id=session_id)


def _tool_defs(*names: str) -> list[dict]:
    return [
        {"type": "function", "function": {"name": n, "description": f"{n} tool", "parameters": {}}}
        for n in names
    ]


def _events_of(stream: list[str], prefix: str) -> list[str]:
    return [e for e in stream if e.startswith(prefix)]


async def _drain(gen) -> list[str]:
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def _payload(event_str: str) -> dict:
    return json.loads(event_str.split("data: ", 1)[1].rstrip("\n"))


# ── Worker unit tests ────────────────────────────────────────────────────


class TestWorker(unittest.IsolatedAsyncioTestCase):
    async def test_no_tools_step_returns_llm_text(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            step = Step(id="s1", goal="summarise prior context", mode="inline")
            tool_set = ToolSet(tool_defs=[], server_tool_names=set(), device_tool_names=set())
            registry = FakeRegistry()
            llm = OrderedFakeLLM([_final("Prior context: user wants news.")])

            result = await run_step_as_worker(
                step,
                upstream_summaries={},
                tool_set=tool_set,
                registry=registry,
                llm=llm,
                session=session,
                user_id="u1",
            )
            self.assertEqual(result.state, "done")
            self.assertIn("news", result.summary)
            self.assertEqual(step.state, "done")
            self.assertEqual(step.result_summary, result.summary)

    async def test_tool_call_then_text(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            step = Step(
                id="s1", goal="search news", mode="inline",
                suggested_tools=["web_search"],
            )
            registry = FakeRegistry(
                tool_defs=_tool_defs("web_search", "calendar_create"),
                server_tools={"web_search", "calendar_create"},
                server_results={"web_search": "headline: rain tomorrow"},
            )
            tool_set = ToolSet(
                tool_defs=registry.tool_defs,
                server_tool_names=registry.server_tools,
                device_tool_names=set(),
            )
            llm = OrderedFakeLLM([
                _tool_response([_tool_call("tc1", "web_search", {"q": "news"})]),
                _final("Search says: rain tomorrow."),
            ])

            result = await run_step_as_worker(
                step,
                upstream_summaries={},
                tool_set=tool_set,
                registry=registry,
                llm=llm,
                session=session,
                user_id="u1",
            )
            self.assertEqual(result.state, "done")
            self.assertIn("rain", result.summary)
            self.assertEqual(registry.server_call_log, [("web_search", {"q": "news"})])
            # Worker restricts tool catalogue to step.suggested_tools, so the
            # LLM call should only have been offered web_search.
            offered = llm.calls[0]["tools"]
            self.assertEqual(len(offered), 1)
            self.assertEqual(offered[0]["function"]["name"], "web_search")

    async def test_llm_failure_returns_failed_result(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            step = Step(id="s1", goal="do a thing", mode="inline")
            tool_set = ToolSet(tool_defs=[], server_tool_names=set(), device_tool_names=set())
            registry = FakeRegistry()
            llm = OrderedFakeLLM([RuntimeError("LLM down")])

            result = await run_step_as_worker(
                step,
                upstream_summaries={},
                tool_set=tool_set,
                registry=registry,
                llm=llm,
                session=session,
                user_id="u1",
            )
            self.assertEqual(result.state, "failed")
            self.assertEqual(step.state, "failed")
            self.assertIn("LLM down", result.summary)

    async def test_device_tool_returns_error_no_deadlock(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            step = Step(
                id="s1", goal="read calendar", mode="inline",
                suggested_tools=["calendar_read"],
            )
            registry = FakeRegistry(
                tool_defs=_tool_defs("calendar_read"),
                device_tools={"calendar_read"},
            )
            tool_set = ToolSet(
                tool_defs=registry.tool_defs,
                server_tool_names=set(),
                device_tool_names={"calendar_read"},
            )
            llm = OrderedFakeLLM([
                _tool_response([_tool_call("tc1", "calendar_read", {})]),
                _final("Could not access calendar."),
            ])

            # Should finish without hanging, and not call registry.execute_server_tool.
            result = await asyncio.wait_for(
                run_step_as_worker(
                    step, upstream_summaries={}, tool_set=tool_set,
                    registry=registry, llm=llm, session=session, user_id="u1",
                ),
                timeout=5.0,
            )
            self.assertEqual(result.state, "done")
            self.assertEqual(registry.server_call_log, [])

    async def test_upstream_summaries_appear_in_system(self):
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            step = Step(id="s2", goal="use prior context", mode="inline")
            tool_set = ToolSet(tool_defs=[], server_tool_names=set(), device_tool_names=set())
            registry = FakeRegistry()
            llm = OrderedFakeLLM([_final("used it")])

            await run_step_as_worker(
                step,
                upstream_summaries={"s1": "found three articles about rain"},
                tool_set=tool_set,
                registry=registry,
                llm=llm,
                session=session,
                user_id="u1",
            )
            system = llm.calls[0]["messages"][0]["content"]
            self.assertIn("three articles", system)


# ── Orchestrator integration tests ───────────────────────────────────────


class TestOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def test_gate_off_delegates_inline(self):
        """Single-tool registry → gate off → no plan, single LLM call, tokens stream."""
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(
                tool_defs=_tool_defs("web_search"),
                server_tools={"web_search"},
            )
            llm = OrderedFakeLLM([_final("hello there")])

            events = await _drain(run_agent_turn("hi", session, registry, llm))

            self.assertEqual(_events_of(events, "event: plan"), [])
            self.assertEqual(_events_of(events, "event: step_start"), [])
            self.assertEqual(len(llm.calls), 1)
            tokens = _events_of(events, "event: token")
            self.assertTrue(tokens, "tokens should stream from the inline path")
            # Assistant reply was appended to history
            self.assertIn("hello there", session.history_jsonl.read_text())

    async def test_single_step_plan_falls_through_to_inline(self):
        """Planner returns a 1-step plan → orchestrator should still use the inline path.

        Expect: plan event emitted (advisory), then inline action loop with
        ONE LLM action call (not a separate synthesizer), tokens stream.
        """
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(
                tool_defs=_tool_defs("web_search", "calendar_create"),
                server_tools={"web_search", "calendar_create"},
            )
            single_step = json.dumps({"steps": [
                {"id": "s1", "goal": "answer", "mode": "inline"},
            ]})
            llm = OrderedFakeLLM([
                _final(single_step),     # planner
                _final("the answer"),    # inline action
            ])

            events = await _drain(run_agent_turn(
                "search news and then stop", session, registry, llm
            ))

            plan_events = _events_of(events, "event: plan")
            self.assertEqual(len(plan_events), 1)
            self.assertEqual(_events_of(events, "event: step_start"), [],
                             "single-step plan should NOT emit step_start")
            self.assertEqual(len(llm.calls), 2, "planner + inline action = 2 LLM calls")
            tokens = _events_of(events, "event: token")
            self.assertTrue(tokens)

    async def test_multi_step_runs_workers_and_synthesizes(self):
        """2-step plan (s2 depends on s1) → sequential waves, then synthesizer."""
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(
                tool_defs=_tool_defs("web_search", "calendar_create"),
                server_tools={"web_search", "calendar_create"},
                server_results={"web_search": "rain tomorrow"},
            )
            plan_json = json.dumps({"steps": [
                {"id": "s1", "goal": "search news", "mode": "inline",
                 "suggested_tools": ["web_search"]},
                {"id": "s2", "goal": "add to calendar", "mode": "inline",
                 "depends_on": ["s1"], "suggested_tools": ["calendar_create"]},
            ]})

            # Call order:
            #   1. planner
            #   2. worker s1 (tool call) — returns web_search tool call
            #   3. worker s1 follow-up (final text)
            #   4. worker s2 (tool call) — returns calendar_create tool call
            #   5. worker s2 follow-up (final text)
            #   6. synthesizer (final)
            responses = [
                _final(plan_json),
                _tool_response([_tool_call("tc_s1", "web_search", {"q": "news"})]),
                _final("Found: rain tomorrow."),
                _tool_response([_tool_call("tc_s2", "calendar_create", {"title": "rain"})]),
                _final("Event created."),
                _final("Here's your summary: found rain news and added to calendar."),
            ]
            llm = OrderedFakeLLM(responses)

            events = await _drain(run_agent_turn(
                "search news and add to calendar", session, registry, llm
            ))

            # Plan emitted
            plan_events = _events_of(events, "event: plan")
            self.assertEqual(len(plan_events), 1)

            # step_start and step_done for each of the 2 steps
            starts = _events_of(events, "event: step_start")
            dones = _events_of(events, "event: step_done")
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(dones), 2)
            self.assertEqual(
                {_payload(e)["step_id"] for e in starts}, {"s1", "s2"},
            )
            self.assertEqual(
                {_payload(e)["step_id"] for e in dones}, {"s1", "s2"},
            )
            for d in dones:
                self.assertEqual(_payload(d)["state"], "done")

            # Both server tools were dispatched
            self.assertEqual(
                {c[0] for c in registry.server_call_log},
                {"web_search", "calendar_create"},
            )

            # Synthesizer ran (final LLM call) and streamed tokens
            tokens = _events_of(events, "event: token")
            self.assertTrue(tokens)
            last_call = llm.calls[-1]
            self.assertIsNone(last_call["tools"],
                              "synthesizer must not be offered tools")
            self.assertIn(
                "SYNTHESIZER", last_call["messages"][0]["content"],
            )
            # Synthesizer saw the step summaries
            user_content = last_call["messages"][1]["content"]
            self.assertIn("[s1]", user_content)
            self.assertIn("[s2]", user_content)
            self.assertIn("rain tomorrow", user_content)

            # Synthesizer's text was appended to history
            history_text = session.history_jsonl.read_text()
            self.assertIn("Here's your summary", history_text)

    async def test_parallel_independent_steps_run_concurrently(self):
        """Two independent steps (no deps) run in the same wave.

        We verify concurrency by counting steps started before any completed:
        with a gate that holds both workers until they've started, both must
        be waiting simultaneously.
        """
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))

            gate = asyncio.Event()
            started = 0
            started_lock = asyncio.Lock()

            class GatedLLM:
                def __init__(self):
                    self.calls: list[dict] = []

                async def chat_completion(self, messages, tools=None):
                    nonlocal started
                    self.calls.append({"messages": messages, "tools": tools})
                    sys = messages[0]["content"]
                    if "PLANNER" in sys:
                        return _final(json.dumps({"steps": [
                            {"id": "s1", "goal": "A", "mode": "inline"},
                            {"id": "s2", "goal": "B", "mode": "inline"},
                        ]}))
                    if "WORKER" in sys:
                        async with started_lock:
                            started += 1
                            reached = started
                        if reached == 1:
                            # First worker waits for the second to arrive.
                            await asyncio.wait_for(gate.wait(), timeout=3.0)
                        else:
                            gate.set()
                        return _final(f"worker-{reached}")
                    return _final("final answer")

            registry = FakeRegistry(
                tool_defs=_tool_defs("tool_a", "tool_b"),
                server_tools={"tool_a", "tool_b"},
            )
            llm = GatedLLM()

            events = await asyncio.wait_for(
                _drain(run_agent_turn(
                    "do A and do B in parallel", session, registry, llm,
                )),
                timeout=5.0,
            )

            self.assertEqual(started, 2, "both workers must have run")
            starts = _events_of(events, "event: step_start")
            self.assertEqual(len(starts), 2)

    async def test_plan_with_device_tool_falls_through_to_inline(self):
        """Multi-step plans that reference a device tool MUST run inline.

        Workers can't own the SSE stream that iOS device tools need, so
        the orchestrator routes such plans to the inline loop where the
        main stream is in scope and the existing device-tool IPC works.
        """
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(
                tool_defs=_tool_defs("web_search", "calendar_create"),
                server_tools={"web_search"},
                device_tools={"calendar_create"},   # ← device tool present
            )
            plan_json = json.dumps({"steps": [
                {"id": "s1", "goal": "search", "mode": "inline",
                 "suggested_tools": ["web_search"]},
                {"id": "s2", "goal": "add to calendar", "mode": "inline",
                 "depends_on": ["s1"], "suggested_tools": ["calendar_create"]},
            ]})
            llm = OrderedFakeLLM([
                _final(plan_json),            # planner
                _final("inline answer"),      # inline action (single call — no worker path)
            ])

            events = await _drain(run_agent_turn(
                "search news and add a calendar event", session, registry, llm
            ))

            # Plan is still emitted (it's advisory).
            self.assertEqual(len(_events_of(events, "event: plan")), 1)
            # But NO worker events — we fell through to inline.
            self.assertEqual(_events_of(events, "event: step_start"), [])
            self.assertEqual(_events_of(events, "event: step_done"), [])
            # Two LLM calls total: planner + inline action (no synthesizer).
            self.assertEqual(len(llm.calls), 2)

    async def test_upstream_failure_marks_downstream_skipped(self):
        """When a step fails, its dependents must not run and the loop must terminate."""
        with tempfile.TemporaryDirectory() as td:
            session = _make_session(Path(td))
            registry = FakeRegistry(
                tool_defs=_tool_defs("tool_a", "tool_b"),
                server_tools={"tool_a", "tool_b"},
            )
            plan_json = json.dumps({"steps": [
                {"id": "s1", "goal": "A", "mode": "inline"},
                {"id": "s2", "goal": "B", "mode": "inline", "depends_on": ["s1"]},
            ]})
            # Worker s1 LLM call raises. Synthesizer call still returns a final.
            call_count = {"n": 0}

            class FailingLLM:
                def __init__(self):
                    self.calls: list[dict] = []

                async def chat_completion(self, messages, tools=None):
                    call_count["n"] += 1
                    self.calls.append({"messages": messages, "tools": tools})
                    sys = messages[0]["content"]
                    if "PLANNER" in sys:
                        return _final(plan_json)
                    if "WORKER" in sys:
                        raise RuntimeError("worker blew up")
                    return _final("partial answer")

            llm = FailingLLM()
            events = await asyncio.wait_for(
                _drain(run_agent_turn(
                    "do A then B", session, registry, llm,
                )),
                timeout=5.0,
            )

            # s1 should be attempted, s2 should NOT be attempted as a worker.
            starts = _events_of(events, "event: step_start")
            self.assertEqual({_payload(e)["step_id"] for e in starts}, {"s1"},
                             "s2 depends on s1 and must not start when s1 fails")

            # Synthesizer ran and produced a final response
            self.assertIn("partial answer", session.history_jsonl.read_text())


if __name__ == "__main__":
    unittest.main()
