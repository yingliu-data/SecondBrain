"""sides — host-originated operations (PLAN.md 8): a `tool_call` event whose
side is not `guest` is a request to the host; the worker carries it out and
answers with a `side_result` control message `{call_id, side, content, ok}`.

  host    registry.execute_server_tool(name, args, allowed) on the asyncio
          loop (skills are async), result sanitised as today
  device  SSE `tool_call{id, name, arguments}` (projected by stream.py from
          the event) and a DeviceCalls entry that `POST /api/v1/tool-result`
          resolves; the entry lives in the registry, not the stream, so it
          survives a dropped SSE connection
  mcp     not in M1: side_result{ok: false, content: "mcp side not available"}

  DeviceCalls              call_id -> open call; `resolve(call_id, result)`
                           is False for an unknown or terminal id (409 at
                           the route); `open_for(session_key)` lists what a
                           re-attaching stream must re-emit
  DEVICE_CALLS             the process-wide registry the route uses
  Sides(...)               `handle_tool_call(event)` schedules the work and
                           returns the asyncio.Task (None for side guest)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from app.agent.loop import _AVATAR_TOOLS
from app.agent.sanitize import sanitize
from app.config import HARNESS_HOST_TOOL_TIMEOUT_S, TOOL_TIMEOUT
from app.skills.base import set_current_context

logger = logging.getLogger("host.sides")


class DeviceCalls:
    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def open(self, call_id: str, name: str, arguments: dict, session_key: str,
             loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Future:
        loop = loop or asyncio.get_running_loop()
        fut = loop.create_future()
        self._open[call_id] = {"future": fut, "name": name, "arguments": arguments,
                               "session_key": session_key, "loop": loop}
        return fut

    def resolve(self, call_id: str, result: str) -> bool:
        """Deliver the device's result. False when the call is unknown or
        already terminal (timed out, resolved, or never opened)."""
        entry = self._open.get(call_id)
        if entry is None:
            return False
        fut: asyncio.Future = entry["future"]
        if fut.done():
            self._open.pop(call_id, None)
            return False
        loop = entry["loop"]
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fut.set_result(result)
        else:
            loop.call_soon_threadsafe(_set_if_pending, fut, result)
        return True

    def discard(self, call_id: str) -> None:
        self._open.pop(call_id, None)

    def is_open(self, call_id: str) -> bool:
        entry = self._open.get(call_id)
        return entry is not None and not entry["future"].done()

    def open_for(self, session_key: str) -> list[dict]:
        return [
            {"id": cid, "name": e["name"], "arguments": e["arguments"]}
            for cid, e in self._open.items()
            if e["session_key"] == session_key and not e["future"].done()
        ]

    def count(self) -> int:
        """Open calls. (No __len__: an empty registry must stay truthy.)"""
        return sum(1 for e in self._open.values() if not e["future"].done())


def _set_if_pending(fut: asyncio.Future, result) -> None:
    if not fut.done():
        fut.set_result(result)


DEVICE_CALLS = DeviceCalls()


class Sides:
    def __init__(self, registry, tenant, session, loop: asyncio.AbstractEventLoop | None, *,
                 send_control: Callable[[str, dict], None],
                 host_event: Callable[[str, dict], object] | None = None,
                 device_calls: DeviceCalls = DEVICE_CALLS,
                 note_host_result: Callable[[str, str], None] | None = None,
                 session_key: str = "",
                 host_timeout_s: float = HARNESS_HOST_TOOL_TIMEOUT_S,
                 device_timeout_s: float = TOOL_TIMEOUT) -> None:
        self.registry = registry
        self.tenant = tenant
        self.session = session
        self.loop = loop
        self.send_control = send_control
        self.host_event = host_event or (lambda t, b: None)
        self.device_calls = device_calls
        self.note_host_result = note_host_result or (lambda c, r: None)
        self.session_key = session_key
        self.host_timeout_s = host_timeout_s
        self.device_timeout_s = device_timeout_s
        self.tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ entry
    def handle_tool_call(self, event: dict) -> asyncio.Task | None:
        body = event["body"]
        side = body["side"]
        if side == "guest":
            return None
        if side == "host":
            coro = self._run_host(body)
        elif side == "device":
            coro = self._run_device(body)
        else:
            coro = self._run_mcp(body)
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _answer(self, body: dict, content: str, ok: bool, started: float) -> dict:
        result = {"call_id": body["call_id"], "side": body["side"], "content": content, "ok": ok}
        self.send_control("side_result", result)
        self.host_event("side_call", {
            "side": body["side"], "call_id": body["call_id"], "name": body["name"],
            "ok": ok, "duration_ms": int((time.monotonic() - started) * 1000),
        })
        return result

    # ------------------------------------------------------------ host
    async def _run_host(self, body: dict) -> dict:
        started = time.monotonic()
        name, args = body["name"], body["args"]
        allowed = self.tenant.allowed_skill_names() if self.tenant is not None else None
        set_current_context(self.session, getattr(self.tenant, "user", ""))
        try:
            raw = await asyncio.wait_for(
                self.registry.execute_server_tool(name, args, allowed),
                timeout=self.host_timeout_s)
        except asyncio.TimeoutError:
            raw = f"Error: {name} timed out after {int(self.host_timeout_s)}s."
        except Exception as e:  # a skill must never take the session down
            logger.exception("host skill %s failed", name)
            raw = f"Error: {name} failed: {e}"
        result = str(raw)
        if name in _AVATAR_TOOLS:
            self.note_host_result(body["call_id"], result)
            if name == "plan_movement":
                # Legacy: feed the model a compact summary, not the frames.
                try:
                    parsed = json.loads(result)
                    result = json.dumps({
                        "status": "ok",
                        "frames_generated": len(parsed.get("frames", [])),
                        "loop": parsed.get("loop", False),
                    })
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
        result = sanitize(result)
        return self._answer(body, result, not result.startswith("Error"), started)

    # ------------------------------------------------------------ device
    async def _run_device(self, body: dict) -> dict:
        started = time.monotonic()
        name, call_id = body["name"], body["call_id"]
        fut = self.device_calls.open(call_id, name, body["args"], self.session_key)
        try:
            raw = await asyncio.wait_for(fut, timeout=self.device_timeout_s)
            result = str(raw) if raw is not None else "No result"
        except asyncio.TimeoutError:
            result = (f"Error: {name} timed out after {int(self.device_timeout_s)}s. "
                      "The iPhone may be unreachable.")
        finally:
            self.device_calls.discard(call_id)
        result = sanitize(result)
        return self._answer(body, result, not result.startswith("Error"), started)

    # ------------------------------------------------------------ mcp
    async def _run_mcp(self, body: dict) -> dict:
        return self._answer(body, "mcp side not available", False, time.monotonic())

    async def cancel_all(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
