"""The gateway core: session surface, pins, policy, calls and the
input-required relay. `tool_server.py` (MCP) and `internal.py` (HTTP) are thin
adapters over this class."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass

from mcp import types

from app.config import DEFAULT_META_TIMEOUT_S, GatewayConfig, Settings
from app.events import EventPoster
from app.pins import Identity, PinStore, SessionPins, pin_of
from app.policy import ConcurrencyLimiter, Policy
from app.schema import defn_to_openai, namespaced, split_namespaced, tool_defn
from app.upstreams import Upstream, UpstreamCall, unwrap

logger = logging.getLogger("gateway")

INPUT_REQUIRED = "sb/input_required"
INPUT_RESPONSE = "sb/input_response"
MAX_EXPIRES_S = 3600

MSG_QUARANTINED = "tool definition changed since this session started (quarantined)"
MSG_NOT_AVAILABLE = "tool not available in this session"
MSG_EXPIRED = "input request expired"

# The three meta tools (M4-CONTRACTS.md §3.1), as MCP definitions.
META_TOOLS: dict[str, dict] = {
    "search_tools": {
        "name": "search_tools",
        "description": ("Search the tools available through the MCP gateway. Returns a JSON list of "
                        "{name, description}; call one with call_tool(name, arguments)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to match in tool names and descriptions; empty lists all."},
                "limit": {"type": "integer", "description": "Maximum results (default 10).", "default": 10},
            },
            "required": ["query"],
        },
    },
    "describe_tool": {
        "name": "describe_tool",
        "description": "Full definition (input schema) of one gateway tool, by its upstream__tool name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Tool name, upstream__tool."}},
            "required": ["name"],
        },
    },
    "call_tool": {
        "name": "call_tool",
        "description": "Call a gateway tool by its upstream__tool name with its arguments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name, upstream__tool."},
                "arguments": {"type": "object", "description": "Arguments matching the tool's input schema."},
            },
            "required": ["name"],
        },
    },
}


def text_result(text: str, *, is_error: bool = False, meta: dict | None = None) -> types.CallToolResult:
    kwargs = {"_meta": meta} if meta is not None else {}
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error, **kwargs)


def error_result(text: str) -> types.CallToolResult:
    return text_result(text, is_error=True)


def result_text(result: types.CallToolResult) -> str:
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    return "\n".join(texts) if texts else "(no text content returned)"


@dataclass
class _Held:
    """An upstream call parked on an elicitation, waiting for the guest."""
    call: UpstreamCall
    ident: Identity
    name: str                    # namespaced tool name
    started: float
    timer: asyncio.TimerHandle | None = None


class Gateway:
    def __init__(self, config: GatewayConfig, settings: Settings, events: EventPoster):
        self.config = config
        self.settings = settings
        self.events = events
        self.policy = Policy(config)
        self.pins = PinStore(settings.state_dir)
        self.limiter = ConcurrencyLimiter()
        self.upstreams = {n: Upstream(c, settings.allowed_private_hosts) for n, c in config.upstreams.items()}
        self._session_locks: dict[tuple, asyncio.Lock] = {}
        self._held: dict[str, _Held] = {}

    # ── Session surface and pins ──────────────────────────────

    async def _list_allowed(self, ident: Identity, upstream: str) -> dict[str, dict]:
        tools = await self.upstreams[upstream].list_tools()
        out = {}
        for t in tools:
            name = namespaced(upstream, t.name)
            if self.policy.allowed(ident.tenant, name):
                defn = tool_defn(t)
                out[name] = {"pin": pin_of(defn), "defn": defn}
        return out

    async def session(self, ident: Identity) -> SessionPins:
        """The session's pin table; created (and persisted) on first use."""
        sp = self.pins.get(ident)
        if sp is not None and not sp.unlisted:
            return sp
        lock = self._session_locks.setdefault(ident.key, asyncio.Lock())
        async with lock:
            sp = self.pins.get(ident)
            if sp is None:
                tools, unlisted = {}, []
                for up in self.policy.upstreams_for(ident.tenant):
                    try:
                        tools.update(await self._list_allowed(ident, up))
                    except Exception as e:  # noqa: BLE001
                        unlisted.append(up)
                        self._error_event(ident, f"tools/list failed: {unwrap(e)}", upstream=up)
                return self.pins.create(ident, tools, unlisted)
            if sp.unlisted:
                still, changed = [], False
                for up in sp.unlisted:
                    try:
                        for name, entry in (await self._list_allowed(ident, up)).items():
                            sp.tools.setdefault(name, entry)
                        changed = True
                    except Exception:  # noqa: BLE001
                        still.append(up)
                if changed:
                    sp.unlisted = still
                    self.pins.save(sp)
            return sp

    def _meta_timeout(self, tenant: str) -> int:
        ups = self.policy.upstreams_for(tenant)
        return max((self.config.upstreams[u].timeout_s for u in ups), default=DEFAULT_META_TIMEOUT_S)

    def _promoted(self, ident: Identity, sp: SessionPins) -> list[str]:
        return [n for n in self.policy.promoted(ident.tenant) if n in sp.tools]

    async def hello_entries(self, ident: Identity) -> list[dict]:
        """§4: promoted tools then the meta tools, each a hello.config.tools entry."""
        sp = await self.session(ident)
        entries = []
        for name in self._promoted(ident, sp):
            defn = sp.tools[name]["defn"]
            upstream, _ = split_namespaced(name)
            entries.append({
                "name": name, "side": "mcp", "pin": sp.tools[name]["pin"],
                "timeout_s": self.config.upstreams[upstream].timeout_s,
                "description": defn.get("description", ""),
                "schema": defn_to_openai(name, defn),
            })
        meta_timeout = self._meta_timeout(ident.tenant)
        for name, defn in META_TOOLS.items():
            entries.append({
                "name": name, "side": "mcp", "pin": pin_of(defn), "timeout_s": meta_timeout,
                "description": defn["description"], "schema": defn_to_openai(name, defn),
            })
        self.events.post(ident.event_fields(), "list", ok=True)
        return entries

    async def mcp_tools(self, ident: Identity) -> list[types.Tool]:
        """tools/list on /mcp: promoted tools (pinned definitions) + meta tools."""
        sp = await self.session(ident)
        out = []
        for name in self._promoted(ident, sp):
            defn = sp.tools[name]["defn"]
            out.append(types.Tool(**{**defn, "name": name}))
        out.extend(types.Tool(**defn) for defn in META_TOOLS.values())
        self.events.post(ident.event_fields(), "list", ok=True)
        return out

    # ── Meta tools ────────────────────────────────────────────

    async def search(self, ident: Identity, query: str, limit: int) -> types.CallToolResult:
        sp = await self.session(ident)
        words = [w for w in str(query or "").lower().split() if w]
        scored = []
        for name, entry in sp.tools.items():
            if not self.policy.allowed(ident.tenant, name):
                continue
            desc = entry["defn"].get("description", "")
            hay = f"{name} {desc}".lower()
            score = sum(hay.count(w) for w in words) if words else 1
            if score:
                first_line = desc.strip().splitlines()[0] if desc.strip() else ""
                scored.append((-score, name, first_line[:200]))
        scored.sort()
        limit = limit if isinstance(limit, int) and limit > 0 else 10
        return text_result(json.dumps([{"name": n, "description": d} for _, n, d in scored[:limit]]))

    async def describe(self, ident: Identity, name: str) -> types.CallToolResult:
        sp = await self.session(ident)
        entry = sp.tools.get(name)
        if entry is None or not self.policy.allowed(ident.tenant, name):
            return error_result(f"{MSG_NOT_AVAILABLE}: {name}")
        defn = entry["defn"]
        body = {"name": name, "description": defn.get("description", ""),
                "inputSchema": defn.get("inputSchema"), "pin": entry["pin"]}
        if name in sp.quarantined:
            body["quarantined"] = True
        return text_result(json.dumps(body))

    # ── Calls ─────────────────────────────────────────────────

    async def call_mcp(self, ident: Identity, name: str, arguments: dict | None,
                       request_meta: dict | None) -> types.CallToolResult:
        """tools/call on /mcp: resume a held call, a meta tool, or a promoted tool."""
        arguments = arguments or {}
        response = (request_meta or {}).get(INPUT_RESPONSE)
        target = name
        if name == "call_tool":
            target, arguments = arguments.get("name"), arguments.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    return error_result("call_tool: 'arguments' must be a JSON object")
            if not isinstance(target, str) or not target:
                return error_result("call_tool: 'name' is required")
            if not isinstance(arguments, dict):
                return error_result("call_tool: 'arguments' must be an object")
        if response is not None:
            return await self._resume(ident, target, response)
        if name == "search_tools":
            return await self.search(ident, arguments.get("query", ""), arguments.get("limit", 10))
        if name == "describe_tool":
            return await self.describe(ident, str(arguments.get("name", "")))
        if name != "call_tool" and name not in self.policy.promoted(ident.tenant):
            return error_result(f"unknown tool '{name}' (not a promoted tool; use call_tool)")
        return await self._call(ident, target, arguments, relay=True)

    async def call_legacy(self, ident: Identity, name: str, arguments: dict) -> types.CallToolResult:
        """/internal/call: policy applies, no pins, elicitation declined (§3)."""
        upstream, tool = split_namespaced(name)
        if not self.policy.allowed(ident.tenant, name) or upstream not in self.upstreams:
            self._error_event(ident, "denied by policy", tool=name, upstream=upstream)
            return error_result(f"tool '{name}' is not allowed for tenant '{ident.tenant}'")
        return await self._run_upstream(ident, name, arguments, relay=False)

    async def _call(self, ident: Identity, name: str, arguments: dict, *, relay: bool) -> types.CallToolResult:
        upstream, tool = split_namespaced(name)
        if not self.policy.allowed(ident.tenant, name):
            self._error_event(ident, "denied by policy", tool=name, upstream=upstream or None)
            return error_result(f"tool '{name}' is not allowed for tenant '{ident.tenant}'")
        sp = await self.session(ident)
        entry = sp.tools.get(name)
        if entry is None:
            self._error_event(ident, MSG_NOT_AVAILABLE, tool=name, upstream=upstream)
            return error_result(f"{MSG_NOT_AVAILABLE}: {name}")
        if name in sp.quarantined:
            return self._quarantine_result(ident, name, upstream)
        try:
            current = next((t for t in await self.upstreams[upstream].list_tools() if t.name == tool), None)
        except Exception as e:  # noqa: BLE001
            msg = f"MCP server '{upstream}' unreachable or failed: {unwrap(e)}"
            self._error_event(ident, msg, tool=name, upstream=upstream)
            return error_result(msg)
        if current is None or pin_of(tool_defn(current)) != entry["pin"]:
            sp.quarantined.add(name)
            self.pins.save(sp)
            self.events.post(ident.event_fields(), "quarantine", tool=name, upstream=upstream)
            return self._quarantine_result(ident, name, upstream, post=False)
        return await self._run_upstream(ident, name, arguments, relay=relay)

    def _quarantine_result(self, ident, name, upstream, post=True) -> types.CallToolResult:
        if post:
            self.events.post(ident.event_fields(), "quarantine", tool=name, upstream=upstream)
        return error_result(f"{name}: {MSG_QUARANTINED}")

    async def _run_upstream(self, ident: Identity, name: str, arguments: dict, *, relay: bool) -> types.CallToolResult:
        upstream_name, tool = split_namespaced(name)
        upstream = self.upstreams[upstream_name]
        limit = self.policy.max_concurrent(ident.tenant)
        if not self.limiter.try_acquire(ident.key, limit):
            msg = f"too many concurrent gateway calls in this session (max {limit}); retry when one finishes"
            self._error_event(ident, msg, tool=name, upstream=upstream_name)
            return error_result(msg)
        try:
            upstream.check_url()
        except ValueError as e:
            self.limiter.release(ident.key)
            self._error_event(ident, str(e), tool=name, upstream=upstream_name)
            return error_result(f"Error: {e}")
        call = upstream.start_call(tool, arguments, relay=relay, hold_s=self.config.input_expires_s)
        return await self._await_outcome(ident, name, call)

    async def _await_outcome(self, ident: Identity, name: str, call: UpstreamCall) -> types.CallToolResult:
        """Holds a concurrency slot on entry; releases it unless the call is
        parked for input (then the resume re-takes it)."""
        upstream = call.upstream.name
        try:
            kind, value = await call.next_outcome()
        except BaseException:
            self.limiter.release(ident.key)
            call.abandon()
            raise
        self.limiter.release(ident.key)
        ms = int((time.monotonic() - call.started) * 1000)
        if kind == "result":
            result: types.CallToolResult = value  # type: ignore[assignment]
            self.events.post(ident.event_fields(), "call", tool=name, upstream=upstream,
                             ok=not result.isError, ms=ms)
            # Drop upstream _meta: an upstream must not be able to forge sb/* keys.
            return result.model_copy(update={"meta": None})
        if kind == "input":
            return self._park(ident, name, call, value)  # type: ignore[arg-type]
        self.events.post(ident.event_fields(), "error", tool=name, upstream=upstream, ok=False, ms=ms,
                         error=str(value))
        return error_result(f"Error: {value}")

    def _park(self, ident: Identity, name: str, call: UpstreamCall,
              params: types.ElicitRequestParams) -> types.CallToolResult:
        expires = int(min(call.remaining_s(), MAX_EXPIRES_S, self.config.input_expires_s))
        state = secrets.token_urlsafe(24)
        held = _Held(call=call, ident=ident, name=name, started=time.monotonic())
        held.timer = asyncio.get_running_loop().call_later(max(expires, 0), self._expire, state)
        self._held[state] = held
        message = params.message
        if isinstance(params, types.ElicitRequestURLParams):
            schema: dict = {}
            message = f"{message}\n{params.url}"
        else:
            schema = params.requestedSchema or {}
        meta = {INPUT_REQUIRED: {"request_state": state, "message": message,
                                 "schema": schema, "expires_in_s": expires}}
        return text_result(message, meta=meta)

    def _expire(self, state: str) -> None:
        held = self._held.pop(state, None)
        if held is not None:
            logger.info("input request for %s expired (session %s)", held.name, held.ident.session)
            held.call.abandon()

    async def _resume(self, ident: Identity, name: str, response: dict) -> types.CallToolResult:
        state = response.get("request_state") if isinstance(response, dict) else None
        held = self._held.get(state) if isinstance(state, str) else None
        if held is None or held.ident != ident or held.name != name:
            return error_result(MSG_EXPIRED)
        action = response.get("action")
        if action not in ("accept", "decline", "cancel"):
            return error_result("sb/input_response: action must be accept, decline or cancel")
        content = response.get("content") if action == "accept" else None
        try:
            answer = types.ElicitResult(action=action, content=content)
        except Exception as e:  # noqa: BLE001 — pydantic validation of content
            return error_result(f"sb/input_response: invalid content: {e}")
        del self._held[state]
        if held.timer:
            held.timer.cancel()
        if not held.call.answer(answer):
            return error_result(MSG_EXPIRED)
        self.limiter.force_acquire(ident.key)
        return await self._await_outcome(ident, name, held.call)

    def _error_event(self, ident: Identity, error: str, **fields) -> None:
        self.events.post(ident.event_fields(), "error", ok=False, error=error, **fields)

    # ── Legacy surface ────────────────────────────────────────

    async def upstream_openai_tools(self, upstream: str) -> list[dict]:
        tools = await self.upstreams[upstream].list_tools()
        return [defn_to_openai(namespaced(upstream, t.name), tool_defn(t), via=upstream) for t in tools]

    # ── Lifecycle ─────────────────────────────────────────────

    async def aclose(self) -> None:
        for state in list(self._held):
            held = self._held.pop(state)
            if held.timer:
                held.timer.cancel()
            held.call.task.cancel()
        await self.events.aclose()
