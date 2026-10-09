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
from protocol.mcp import (
    ACTIONS,
    INPUT_REQUIRED,
    INPUT_RESPONSE,
    META_TOOLS,
    namespaced,
    split_namespaced,
)

from app.config import DEFAULT_META_TIMEOUT_S, GatewayConfig, Settings
from app.events import EventPoster
from app.pins import Identity, PinStore, SessionPins, pin_of
from app.policy import ConcurrencyLimiter, Policy
from app.schema import defn_to_openai
from app.upstreams import Upstream, UpstreamCall, unwrap

logger = logging.getLogger("gateway")

MAX_EXPIRES_S = 3600
SEARCH_TOOLS, DESCRIBE_TOOL, CALL_TOOL = META_TOOLS

MSG_QUARANTINED = "tool definition changed since this session started (quarantined)"
MSG_NOT_AVAILABLE = "tool not available in this session"
MSG_EXPIRED = "input request expired"

# The three meta tools (M4-CONTRACTS.md §3.1), as MCP definitions, and their pins.
META_DEFNS: dict[str, dict] = {
    SEARCH_TOOLS: {
        "name": SEARCH_TOOLS,
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
    DESCRIBE_TOOL: {
        "name": DESCRIBE_TOOL,
        "description": "Full definition (input schema) of one gateway tool, by its upstream__tool name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Tool name, upstream__tool."}},
            "required": ["name"],
        },
    },
    CALL_TOOL: {
        "name": CALL_TOOL,
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
META_PINS: dict[str, str] = {name: pin_of(defn) for name, defn in META_DEFNS.items()}


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
    timer: asyncio.TimerHandle | None = None


class Gateway:
    def __init__(self, config: GatewayConfig, settings: Settings, events: EventPoster):
        self.config = config
        self.settings = settings
        self.events = events
        self.policy = Policy(config)
        self.pins = PinStore(settings.state_dir)
        self.limiter = ConcurrencyLimiter()
        self.upstreams = {n: Upstream(c, settings.allowed_private_hosts, config.input_expires_s)
                          for n, c in config.upstreams.items()}
        self._session_locks: dict[tuple, asyncio.Lock] = {}
        self._save_locks: dict[tuple, asyncio.Lock] = {}
        self._held: dict[str, _Held] = {}

    # ── Session surface and pins ──────────────────────────────

    async def _list_allowed(self, ident: Identity, upstream: str) -> dict[str, dict]:
        out = {}
        for tool, (defn, pin) in (await self.upstreams[upstream].tools()).items():
            name = namespaced(upstream, tool)
            if self.policy.allowed(ident.tenant, name):
                out[name] = {"pin": pin, "defn": defn}
        return out

    async def _save(self, sp: SessionPins) -> None:
        # one write at a time per session, the snapshot taken inside the lock:
        # an older snapshot can never land after a newer one
        async with self._save_locks.setdefault(sp.ident.key, asyncio.Lock()):
            await asyncio.to_thread(self.pins.write, sp.ident, sp.to_json())

    async def session(self, ident: Identity) -> SessionPins:
        """The session's pin table; created (and persisted) on first use.
        Upstreams that could not be listed are retried on later uses."""
        sp = self.pins.cached(ident)
        if sp is not None and not sp.unlisted:
            return sp
        async with self._session_locks.setdefault(ident.key, asyncio.Lock()):
            sp = self.pins.cached(ident) or await asyncio.to_thread(self.pins.get, ident)
            if sp is not None and not sp.unlisted:
                return sp
            pending = sp.unlisted if sp is not None else self.policy.upstreams_for(ident.tenant)
            listed = await asyncio.gather(*(self._list_allowed(ident, up) for up in pending),
                                          return_exceptions=True)
            tools = sp.tools if sp is not None else {}
            unlisted = []
            for up, res in zip(pending, listed):
                if isinstance(res, Exception):
                    unlisted.append(up)
                    if sp is None:   # retries fail fast (upstream failure cache); report once
                        self._error_event(ident, f"tools/list failed: {unwrap(res)}", upstream=up)
                elif isinstance(res, BaseException):
                    raise res
                else:
                    for name, entry in res.items():
                        tools.setdefault(name, entry)   # an existing pin never changes
            if sp is None:
                return await asyncio.to_thread(self.pins.create, ident, tools, unlisted)
            if unlisted != sp.unlisted:
                sp.unlisted = unlisted
                await self._save(sp)
            return sp

    def _meta_timeout(self, tenant: str) -> int:
        ups = self.policy.upstreams_for(tenant)
        return max((self.config.upstreams[u].timeout_s for u in ups), default=DEFAULT_META_TIMEOUT_S)

    async def _surface(self, ident: Identity) -> list[tuple[str, dict, str, int]]:
        """§4: promoted tools then the meta tools, as (name, defn, pin, timeout_s).
        Posts the `list` event."""
        sp = await self.session(ident)
        out = []
        for name in self.policy.promoted(ident.tenant):
            if name in sp.tools:
                entry = sp.tools[name]
                timeout = self.config.upstreams[split_namespaced(name)[0]].timeout_s
                out.append((name, entry["defn"], entry["pin"], timeout))
        meta_timeout = self._meta_timeout(ident.tenant)
        out.extend((name, defn, META_PINS[name], meta_timeout) for name, defn in META_DEFNS.items())
        self.events.post(ident.event_fields(), "list", ok=True)
        return out

    async def hello_entries(self, ident: Identity) -> list[dict]:
        """The surface as hello.config.tools entries."""
        return [{"name": name, "side": "mcp", "pin": pin, "timeout_s": timeout,
                 "description": defn.get("description", ""), "schema": defn_to_openai(name, defn)}
                for name, defn, pin, timeout in await self._surface(ident)]

    async def mcp_tools(self, ident: Identity) -> list[types.Tool]:
        """The surface as tools/list on /mcp (pinned definitions)."""
        return [types.Tool(**{**defn, "name": name}) for name, defn, _, _ in await self._surface(ident)]

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
        if name == CALL_TOOL:
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
        if name == SEARCH_TOOLS:
            return await self.search(ident, arguments.get("query", ""), arguments.get("limit", 10))
        if name == DESCRIBE_TOOL:
            return await self.describe(ident, str(arguments.get("name", "")))
        if name != CALL_TOOL and name not in self.policy.promoted(ident.tenant):
            return error_result(f"unknown tool '{name}' (not a promoted tool; use call_tool)")
        return await self._call(ident, target, arguments, relay=True)

    async def call_legacy(self, ident: Identity, name: str, arguments: dict) -> types.CallToolResult:
        """/internal/call: policy applies, no pins, elicitation declined (§3)."""
        upstream, _ = split_namespaced(name)
        if not self.policy.allowed(ident.tenant, name):
            self._error_event(ident, "denied by policy", tool=name, upstream=upstream or None)
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
            current = (await self.upstreams[upstream].tools()).get(tool)
        except Exception as e:  # noqa: BLE001
            msg = f"MCP server '{upstream}' unreachable or failed: {unwrap(e)}"
            self._error_event(ident, msg, tool=name, upstream=upstream)
            return error_result(msg)
        if current is None or current[1] != entry["pin"]:
            sp.quarantined.add(name)
            await self._save(sp)
            return self._quarantine_result(ident, name, upstream)
        return await self._run_upstream(ident, name, arguments, relay=relay)

    def _quarantine_result(self, ident: Identity, name: str, upstream: str) -> types.CallToolResult:
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
            return error_result(str(e))
        call = UpstreamCall(upstream, tool, arguments, relay=relay)
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
        return error_result(str(value))

    def _park(self, ident: Identity, name: str, call: UpstreamCall,
              params: types.ElicitRequestParams) -> types.CallToolResult:
        expires = int(min(call.remaining_s(), MAX_EXPIRES_S, self.config.input_expires_s))
        state = secrets.token_urlsafe(24)
        held = _Held(call=call, ident=ident, name=name)
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
        if action not in ACTIONS:
            return error_result(f"{INPUT_RESPONSE}: action must be one of {', '.join(ACTIONS)}")
        content = response.get("content") if action == "accept" else None
        try:
            answer = types.ElicitResult(action=action, content=content)
        except Exception as e:  # noqa: BLE001 — pydantic validation of content
            return error_result(f"{INPUT_RESPONSE}: invalid content: {e}")
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
        tools = await self.upstreams[upstream].tools()
        return [defn_to_openai(namespaced(upstream, tool), defn, via=upstream) for tool, (defn, _) in tools.items()]

    # ── Lifecycle ─────────────────────────────────────────────

    async def aclose(self) -> None:
        for state in list(self._held):
            held = self._held.pop(state)
            if held.timer:
                held.timer.cancel()
            held.call.task.cancel()
        await asyncio.gather(*(u.aclose() for u in self.upstreams.values()))
        await self.events.aclose()
