"""M4 worker side: hello tool entries from the gateway (up, down, cached for
resume), legacy mcp_* skills kept out of host entries, the :5011 forwarder's
lifecycle, and `lost` discarding a held call."""
import asyncio

import pytest

from app.host.endpoints import socket_path
from app.host.sides import DeviceCalls
from app.host.worker import Worker
from gateway_fakes import FakeGateway
from host_fakes import (FakeRegistry, FakeSandbox, FakeTenant, session, users_root,  # noqa: F401
                        worker_kwargs)

ENTRY = {"name": "wcc-events__draft_event", "side": "mcp", "pin": "a" * 64, "timeout_s": 2700,
         "description": "draft",
         "schema": {"type": "function", "function": {"name": "wcc-events__draft_event",
                                                     "description": "draft",
                                                     "parameters": {"type": "object"}}}}
META = dict(ENTRY, name="call_tool", pin="b" * 64,
            schema={"type": "function", "function": {"name": "call_tool", "description": "",
                                                     "parameters": {"type": "object"}}})


def fn(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


class SkillRegistry(FakeRegistry):
    """Skills by name, with a legacy MCP skill registered like main.py does."""

    skills = {"web_search": ["web_search"], "mcp_wcc-events": ["wcc-events__draft_event"]}

    def skill_names(self):
        return list(self.skills)

    def _names(self, allowed):
        return [t for s, tools in self.skills.items()
                if allowed is None or s in allowed for t in tools]

    def get_active_tool_definitions(self, allowed=None):
        return [fn(n) for n in self._names(allowed)]

    def get_server_tool_names(self, allowed=None):
        return set(self._names(allowed))

    def get_device_tool_names(self, allowed=None):
        return set()


@pytest.fixture
def gateway():
    gw = FakeGateway().start()
    gw.tools = [ENTRY, META]
    yield gw
    gw.stop()


@pytest.mark.asyncio
async def test_hello_appends_gateway_entries_and_drops_legacy_mcp_skills(session, gateway):
    sandbox = FakeSandbox([{"text": "ok"}])
    w = Worker(session, FakeTenant(), SkillRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs(gateway_url=gateway.url))
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done"
    tools = w.last_hello["config"]["tools"]
    assert [t["name"] for t in tools] == ["web_search", "wcc-events__draft_event", "call_tool"]
    assert tools[0]["side"] == "host"
    assert tools[1] == ENTRY and tools[2] == META            # verbatim
    req = next(r for r in gateway.requests if r["path"] == "/internal/tools")
    h = req["headers"]
    assert (h["x-sb-user"], h["x-sb-tenant"], h["x-sb-session"]) == ("u", "t", "s1")
    assert "gateway" not in w.last_hello["config"]["ports"]
    assert socket_path(session.root, 5011).exists() and w.gateway is not None
    await w.close()
    assert not socket_path(session.root, 5011).exists()


@pytest.mark.asyncio
async def test_restricted_tenant_never_offers_mcp_skills_as_host_tools(session, gateway):
    w = Worker(session, FakeTenant(local_skills=["web_search", "mcp_wcc-events"]),
               SkillRegistry(), FakeSandbox([]), **worker_kwargs(gateway_url=gateway.url))
    assert [t["name"] for t in w.tool_entries()] == ["web_search"]


@pytest.mark.asyncio
async def test_gateway_down_starts_without_mcp_tools(session):
    sandbox = FakeSandbox([{"text": "ok"}])
    w = Worker(session, FakeTenant(), SkillRegistry(), sandbox, device_calls=DeviceCalls(),
               **worker_kwargs(gateway_url="http://127.0.0.1:9", gateway_tools_timeout_s=2))
    result = await w.act({"input_id": "in_1", "text": "hi"})
    assert result["outcome"] == "done"
    assert [t["name"] for t in w.last_hello["config"]["tools"]] == ["web_search"]
    assert w.gateway_entries is None                         # the next boot tries again
    await w.close()


@pytest.mark.asyncio
async def test_entries_are_cached_for_a_resume_hello(session, gateway):
    w = Worker(session, FakeTenant(), SkillRegistry(), FakeSandbox([]),
               **worker_kwargs(gateway_url=gateway.url))
    first = await w.load_gateway_entries()
    gateway.stop()                                          # the gateway goes down
    again = await w.load_gateway_entries()
    assert first == again == [ENTRY, META]
    assert w.build_hello(resume=True)["config"]["tools"][-2:] == [ENTRY, META]
    assert sum(r["path"] == "/internal/tools" for r in gateway.requests) == 1


@pytest.mark.asyncio
async def test_lost_mcp_call_discards_its_held_entry(session, gateway):
    w = Worker(session, FakeTenant(), SkillRegistry(), FakeSandbox([]),
               **worker_kwargs(gateway_url=gateway.url))
    w._ensure_plumbing()
    entry = w.gateway_relay.held("call_9", False, b'{"id": 1}', {})
    assert entry.done.wait(5)
    assert w.gateway_relay.pending() == {"call_9": "done"}
    w._on_lost("call_9")
    assert w.gateway_relay.pending() == {}
    # park closes :5011 but keeps the relay; the wake opens a new forwarder
    w._close_guest_ports()
    assert w.gateway is None and not socket_path(session.root, 5011).exists()
    relay = w.gateway_relay
    w._open_guest_ports()
    assert w.gateway is not None and w.gateway_relay is relay
    await asyncio.sleep(0)
    await w.close()
