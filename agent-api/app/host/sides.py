"""sides — host-originated operations: a `tool_call` event whose
side is not `guest` is a request to the host; the worker carries it out and
answers with a `side_result` control message `{call_id, side, content, ok,
memory?}`.

  host    registry.execute_server_tool(name, args, allowed) on the asyncio
          loop (skills are async), result sanitised as today; an ok
          `remember`/`forget` result adds `memory: {scope, op: write|remove,
          slug, sha256}` to the side_result (the guest emits its `memory`
          event from it verbatim) and the same dict, plus call_id, is logged
          as a `memory` host event (sha256 = sha256_hex(body.strip())
          from the skill's result JSON, "" for a removal)
  device  SSE `tool_call{id, name, arguments}` (projected by stream.py from
          the event) and a DeviceCalls entry that `POST /api/v1/tool-result`
          resolves; the entry lives in the registry, not the stream, so it
          survives a dropped SSE connection
  mcp     not the host's: the guest calls the gateway itself over :5011
          (gateway_forwarder.py); an mcp call never becomes a side_result,
          so handle_tool_call returns None as for guest

  DeviceCalls              call_id -> open call; `resolve(call_id, result)`
                           is False for an unknown or terminal id (409 at
                           the route); `open_for(session_key)` lists what a
                           re-attaching stream must re-emit. `adopt(...)`:
                           an open call rebuilt from a parked session's log
                           by a new Worker (an agent-api restart): no waiter
                           in this process, its `deliver(result)` carries the
                           result to the guest (Worker._deliver_adopted)
  DEVICE_CALLS             the process-wide registry the route uses
  Sides(...)               `handle_tool_call(event)` schedules the work and
                           returns the asyncio.Task (None for side guest)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from app.host.sanitize import sanitize
from app.skills.avatar_control import AVATAR_TOOLS
from app.config import HARNESS_HOST_TOOL_TIMEOUT_S, TOOL_TIMEOUT
from app.skills.base import set_current_context

logger = logging.getLogger("host.sides")

MEMORY_TOOLS = {"remember": "write", "forget": "remove"}


class DeviceCalls:
    """Open device calls by call_id. An entry has a `future` (a live
    Sides._run_device waits on it) or, adopted after a restart, a `deliver`
    callback instead (future None)."""

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def open(self, call_id: str, name: str, arguments: dict, session_key: str,
             loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Future:
        loop = loop or asyncio.get_running_loop()
        fut = loop.create_future()
        self._open[call_id] = {"future": fut, "name": name, "arguments": arguments,
                               "session_key": session_key, "loop": loop}
        return fut

    def adopt(self, call_id: str, name: str, arguments: dict, session_key: str,
              deliver: Callable[[str], None],
              loop: asyncio.AbstractEventLoop | None = None) -> None:
        """An open call no Sides waits on: the session's log has it open
        (a new Worker rebuilt it from the fold). resolve() hands the result
        to `deliver` (on `loop`, when given) and the call is terminal."""
        self._open[call_id] = {"future": None, "deliver": deliver, "name": name,
                               "arguments": arguments, "session_key": session_key,
                               "loop": loop}

    @staticmethod
    def _pending(entry: dict) -> bool:
        return entry["future"] is None or not entry["future"].done()

    def resolve(self, call_id: str, result: str, owner_prefix: str | None = None) -> bool:
        """Deliver the device's result. False when the call is unknown or
        already terminal (timed out, resolved, or never opened), or, with
        `owner_prefix`, belongs to a session whose key does not start with it
        (another tenant's call)."""
        entry = self._open.get(call_id)
        if entry is None:
            return False
        if owner_prefix is not None and not entry["session_key"].startswith(owner_prefix):
            return False
        if not self._pending(entry):
            self._open.pop(call_id, None)
            return False
        loop = entry["loop"]
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        fut: asyncio.Future | None = entry["future"]
        if fut is None:                     # adopted: one result, then terminal
            self._open.pop(call_id, None)
            if loop is None or running is loop:
                entry["deliver"](result)
            else:
                loop.call_soon_threadsafe(entry["deliver"], result)
        elif running is loop:
            fut.set_result(result)
        else:
            loop.call_soon_threadsafe(_set_if_pending, fut, result)
        return True

    def discard(self, call_id: str) -> None:
        self._open.pop(call_id, None)

    def release(self, call_id: str, deliver: Callable[[str], None]) -> None:
        """Drop an adopted call if `deliver` still owns it (a closing Worker:
        a newer Worker may have adopted the same call meanwhile)."""
        entry = self._open.get(call_id)
        if entry is not None and entry.get("deliver") is deliver:
            self._open.pop(call_id, None)

    def is_open(self, call_id: str) -> bool:
        entry = self._open.get(call_id)
        return entry is not None and self._pending(entry)

    def open_for(self, session_key: str) -> list[dict]:
        return [
            {"id": cid, "name": e["name"], "arguments": e["arguments"]}
            for cid, e in self._open.items()
            if e["session_key"] == session_key and self._pending(e)
        ]

    def count(self) -> int:
        """Open calls. (No __len__: an empty registry must stay truthy.)"""
        return sum(1 for e in self._open.values() if self._pending(e))


def _memory_change(name: str, args: dict, result: str) -> dict | None:
    """`{scope, op, slug, sha256}` of an ok remember/forget result; None for
    any other tool, a non-JSON result or `not_found` (nothing changed)."""
    if name not in MEMORY_TOOLS:
        return None
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("status") != "ok":
        return None
    return {"scope": parsed.get("scope") or args.get("scope", "user"),
            "op": MEMORY_TOOLS[name],
            "slug": parsed.get("slug") or args.get("slug", ""),
            "sha256": parsed.get("sha256", "")}


def device_content(raw) -> tuple[str, bool]:
    """A device's raw result as the side_result `(content, ok)`."""
    content = sanitize(str(raw) if raw is not None else "No result")
    return content, not content.startswith("Error")


def device_timeout(name: str, timeout_s: float) -> str:
    return (f"Error: {name} timed out after {int(timeout_s)}s. "
            "The iPhone may be unreachable.")


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
        if side == "host":
            coro = self._run_host(body)
        elif side == "device":
            coro = self._run_device(body)
        else:
            return None        # guest, or mcp (the guest's own gateway call)
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _answer(self, body: dict, content: str, ok: bool, started: float,
                memory: dict | None = None) -> dict:
        result = {"call_id": body["call_id"], "side": body["side"], "content": content, "ok": ok}
        if memory is not None:
            result["memory"] = memory
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
        set_current_context(self.session, getattr(self.tenant, "user", ""),
                            getattr(self.tenant, "name", ""))
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
        if name in AVATAR_TOOLS:
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
        ok = not result.startswith("Error")
        memory = _memory_change(name, args, result) if ok else None
        if memory is not None:
            self.host_event("memory", {**memory, "call_id": body["call_id"]})
        return self._answer(body, result, ok, started, memory)

    # ------------------------------------------------------------ device
    async def _run_device(self, body: dict) -> dict:
        started = time.monotonic()
        name, call_id = body["name"], body["call_id"]
        fut = self.device_calls.open(call_id, name, body["args"], self.session_key)
        try:
            raw = await asyncio.wait_for(fut, timeout=self.device_timeout_s)
        except asyncio.TimeoutError:
            raw = device_timeout(name, self.device_timeout_s)
        finally:
            self.device_calls.discard(call_id)
        return self._answer(body, *device_content(raw), started)

    async def cancel_all(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
