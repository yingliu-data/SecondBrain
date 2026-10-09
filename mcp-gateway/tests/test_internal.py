"""Bearer, identity headers, /internal/* routes, /healthz."""

import pytest
from conftest import IDENT, make_config

from app.gateway import META_DEFNS
from app.pins import pin_of


async def test_healthz_needs_nothing(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        r = await gw.client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("path,method", [("/mcp", "POST"), ("/internal/tools", "GET")])
async def test_bearer_required_everywhere(upstream, harness, path, method):
    async with harness(make_config(upstream.url)) as gw:
        r = await gw.internal(method, path, token=None)
        assert r.status_code == 401
        r = await gw.internal(method, path, token="wrong")
        assert r.status_code == 401


async def test_no_token_configured_is_dev_mode(upstream, harness):
    async with harness(make_config(upstream.url), token="") as gw:
        r = await gw.rpc("tools/list", token=None)
        assert r.status_code == 200


@pytest.mark.parametrize("bad", [
    {"X-SB-User": "alice", "X-SB-Tenant": "wcc-event"},                       # missing session
    {**IDENT, "X-SB-User": ".."},
    {**IDENT, "X-SB-Tenant": "."},
    {**IDENT, "X-SB-Session": "a/b"},
    {**IDENT, "X-SB-Session": "x" * 129},
    {**IDENT, "X-SB-User": "al ice"},
    {**IDENT, "X-SB-User": "alice@example.com"},                               # agent-api's rule: no '@' or '.'
    {**IDENT, "X-SB-Session": "x" * 65},
])
async def test_identity_headers_400(upstream, harness, bad):
    async with harness(make_config(upstream.url)) as gw:
        assert (await gw.rpc("tools/list", ident=bad)).status_code == 400
        assert (await gw.internal("GET", "/internal/tools", ident=bad)).status_code == 400


async def test_internal_tools_hello_shape(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        r = await gw.internal("GET", "/internal/tools")
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert [t["name"] for t in tools] == ["fake__echo", "search_tools", "describe_tool", "call_tool"]
        echo = tools[0]
        assert echo["side"] == "mcp" and echo["timeout_s"] == 30 and len(echo["pin"]) == 64
        assert echo["description"] == "Echo the text back."
        fn = echo["schema"]["function"]
        assert echo["schema"]["type"] == "function" and fn["name"] == "fake__echo"
        assert set(fn["parameters"]["properties"]) == {"text"}
        meta = {t["name"]: t for t in tools[1:]}
        for name, defn in META_DEFNS.items():
            assert meta[name]["pin"] == pin_of(defn) and meta[name]["timeout_s"] == 30
        # Later calls return the same pins.
        assert (await gw.internal("GET", "/internal/tools")).json()["tools"] == tools


async def test_tenant_without_policy_gets_meta_only(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        stranger = {**IDENT, "X-SB-Tenant": "nobody"}
        tools = (await gw.internal("GET", "/internal/tools", ident=stranger)).json()["tools"]
        assert [t["name"] for t in tools] == list(META_DEFNS)
        res = await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 1, "b": 1}}, ident=stranger)
        assert res["isError"] is True


@pytest.mark.parametrize("path,method", [("/internal/call", "POST"), ("/internal/upstreams/fake/tools", "GET")])
async def test_legacy_routes_are_gone(upstream, harness, path, method):
    """The legacy engine's routes were deleted with the engine."""
    async with harness(make_config(upstream.url)) as gw:
        r = await gw.internal(method, path, json={"name": "fake__add", "arguments": {}})
        assert r.status_code in (404, 405)
