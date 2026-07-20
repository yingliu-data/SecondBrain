import pytest

from app.mcp import schema, ssrf
from app.mcp.proxy_skill import MCPProxySkill
from app.tenants import MCPServerConfig


class FakeTool:
    def __init__(self, name, description=None, inputSchema=None):
        self.name, self.description, self.inputSchema = name, description, inputSchema


def test_conversion_namespaces_and_defaults():
    d = schema.mcp_tool_to_openai("wcc", FakeTool("draft_event"))
    fn = d["function"]
    assert fn["name"] == "wcc__draft_event"
    assert fn["parameters"] == {"type": "object", "properties": {}}
    assert "[via wcc MCP]" in fn["description"]


def test_conversion_keeps_schema_strips_dollar_schema():
    s = {"type": "object", "properties": {"x": {"type": "string"}},
         "$schema": "http://json-schema.org/draft-07/schema#"}
    d = schema.mcp_tool_to_openai("wcc", FakeTool("t", "desc", s))
    assert "$schema" not in d["function"]["parameters"]
    assert d["function"]["parameters"]["properties"] == {"x": {"type": "string"}}


def test_split_namespaced_first_separator_only():
    assert schema.split_namespaced("wcc__do__thing") == ("wcc", "do__thing")


def _cfg(url, **kw):
    return MCPServerConfig(name="t", url=url, **kw)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/mcp",
    "http://10.0.0.5/mcp",
    "http://[::1]/mcp",
    "http://169.254.1.1/mcp",
])
def test_ssrf_blocks_private(url):
    with pytest.raises(ValueError):
        ssrf.validate_mcp_url(_cfg(url))


def test_ssrf_allow_private_optin():
    ssrf.validate_mcp_url(_cfg("http://127.0.0.1:8765/mcp", allow_private=True))


def test_ssrf_allowlisted_hostname():
    ssrf.validate_mcp_url(_cfg("http://host.docker.internal:8765/mcp"))


def test_ssrf_rejects_bad_scheme():
    with pytest.raises(ValueError):
        ssrf.validate_mcp_url(_cfg("ftp://example.com/mcp"))


@pytest.mark.asyncio
async def test_proxy_execute_strips_namespace(monkeypatch):
    cfg = MCPServerConfig(name="wcc", url="http://host.docker.internal:8765/mcp",
                          allow_private=True)
    skill = MCPProxySkill(cfg)
    seen = {}

    async def fake_call(cfg_, tool, args):
        seen.update(tool=tool, args=args)
        return "remote-ok"

    from app.mcp import client
    monkeypatch.setattr(client, "call_tool", fake_call)
    result = await skill.execute("wcc__draft_event", {"raw_notes": "x"})
    assert result == "remote-ok"
    assert seen == {"tool": "draft_event", "args": {"raw_notes": "x"}}


@pytest.mark.asyncio
async def test_proxy_execute_rejects_foreign_tool():
    cfg = MCPServerConfig(name="wcc", url="http://host.docker.internal:8765/mcp")
    skill = MCPProxySkill(cfg)
    result = await skill.execute("other__tool", {})
    assert result.startswith("Error:")
