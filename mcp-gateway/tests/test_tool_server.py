"""POST /mcp: stateless tools/list and tools/call, meta tools, promoted tools,
pins, quarantine, concurrency."""
import asyncio
import json

from conftest import IDENT, UPSTREAM_TOKEN, make_config, text_of

from app.gateway import META_DEFNS
from app.pins import pin_of


async def test_tools_list_without_initialize(upstream, harness, agent_api):
    async with harness(make_config(upstream.url)) as gw:
        r = await gw.rpc("tools/list")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        names = [t["name"] for t in r.json()["result"]["tools"]]
        assert names == ["fake__echo", "search_tools", "describe_tool", "call_tool"]
        await gw.events.drain()
    assert agent_api.ops("list")[0] == {"user": "alice", "tenant": "wcc-event",
                                       "session_id": IDENT["X-SB-Session"], "op": "list", "ok": True}


async def test_promoted_direct_call_and_upstream_auth(upstream, harness, agent_api):
    async with harness(make_config(upstream.url)) as gw:
        res = await gw.call("fake__echo", {"text": "hi"})
        assert res["isError"] is False
        assert text_of(res) == "echo: hi"
        await gw.events.drain()
    assert upstream.auth_headers[-1] == f"Bearer {UPSTREAM_TOKEN}"
    call = agent_api.ops("call")[0]
    assert call["tool"] == "fake__echo" and call["upstream"] == "fake" and call["ok"] is True
    assert isinstance(call["ms"], int)
    assert agent_api.auth and all(a == "Bearer test-internal-token" for a in agent_api.auth)


async def test_non_promoted_direct_call_refused(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        res = await gw.call("fake__add", {"a": 1, "b": 2})
        assert res["isError"] is True and "call_tool" in text_of(res)


async def test_search_describe_call_tool(upstream, harness):
    async with harness(make_config(upstream.url, deny=["fake__slow"])) as gw:
        found = json.loads(text_of(await gw.call("search_tools", {"query": "integers"})))
        assert found == [{"name": "fake__add", "description": "Add two integers."}]
        everything = json.loads(text_of(await gw.call("search_tools", {"query": "", "limit": 50})))
        names = {f["name"] for f in everything}
        assert "fake__slow" not in names and {"fake__echo", "fake__add", "fake__approve"} <= names
        assert len(json.loads(text_of(await gw.call("search_tools", {"query": "", "limit": 2})))) == 2

        desc = json.loads(text_of(await gw.call("describe_tool", {"name": "fake__add"})))
        assert desc["name"] == "fake__add"
        assert set(desc["inputSchema"]["properties"]) == {"a", "b"}
        assert len(desc["pin"]) == 64

        res = await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 2, "b": 3}})
        assert res["isError"] is False and text_of(res) == "5"

        denied = await gw.call("call_tool", {"name": "fake__slow", "arguments": {}})
        assert denied["isError"] is True and "not allowed" in text_of(denied)
        missing = await gw.call("describe_tool", {"name": "fake__slow"})
        assert missing["isError"] is True


async def test_upstream_error_is_result(upstream, harness, agent_api):
    async with harness(make_config(upstream.url)) as gw:
        res = await gw.call("call_tool", {"name": "fake__boom"})
        assert res["isError"] is True and "kaboom" in text_of(res)
        res = await gw.call("call_tool", {"name": "fake__nope"})
        assert res["isError"] is True and "not available in this session" in text_of(res)
        res = await gw.call("call_tool", {"name": "elsewhere__x"})
        assert res["isError"] is True


async def test_unreachable_upstream(harness, agent_api):
    async with harness(make_config("http://127.0.0.1:9/mcp")) as gw:
        r = await gw.rpc("tools/list")
        assert [t["name"] for t in r.json()["result"]["tools"]] == list(META_DEFNS)
        res = await gw.call("call_tool", {"name": "fake__echo", "arguments": {"text": "x"}})
        assert res["isError"] is True
        await gw.events.drain()
    assert agent_api.ops("error")


async def test_pins_persist_across_restart(upstream, harness, tmp_path):
    cfg = make_config(upstream.url)
    async with harness(cfg) as gw:
        r = await gw.internal("GET", "/internal/tools")
        first = r.json()["tools"]
        pin_file = tmp_path / "state" / "pins" / "alice" / "wcc-event" / f"{IDENT['X-SB-Session']}.json"
        table = json.loads(pin_file.read_text())
        assert {"fake__echo", "fake__add", "fake__approve", "fake__slow"} <= set(table["tools"])
        assert "fake__boom_hidden" not in table["tools"]
        stored = table["tools"]["fake__echo"]
        assert stored["pin"] == pin_of(stored["defn"])

    upstream.change_echo()
    # "Restart": a new app over the same state dir. The session keeps its
    # pins (so echo is quarantined); a new session pins the new definition.
    async with harness(cfg) as gw2:
        again = (await gw2.internal("GET", "/internal/tools")).json()["tools"]
        assert again == first
        desc = json.loads(text_of(await gw2.call("describe_tool", {"name": "fake__echo"})))
        assert desc["description"] == "Echo the text back."
        res = await gw2.call("fake__echo", {"text": "x"})
        assert res["isError"] is True and "quarantined" in text_of(res)

        other = {**IDENT, "X-SB-Session": "sess_new"}
        res = await gw2.call("fake__echo", {"text": "x"}, ident=other)
        assert res["isError"] is False and text_of(res) == "ECHO: X"
        new_tools = (await gw2.internal("GET", "/internal/tools", ident=other)).json()["tools"]
        assert new_tools[0]["pin"] != first[0]["pin"]


async def test_quarantine_mid_session(upstream, harness, agent_api):
    async with harness(make_config(upstream.url)) as gw:
        assert (await gw.call("fake__echo", {"text": "a"}))["isError"] is False
        upstream.change_echo()
        upstream.add_tool()
        await gw.gateway.upstreams["fake"].refresh()   # what the TTL's background refresh does
        res = await gw.call("fake__echo", {"text": "b"})
        assert res["isError"] is True
        assert "tool definition changed since this session started (quarantined)" in text_of(res)
        # Sticky even through call_tool, and other tools still work.
        assert (await gw.call("call_tool", {"name": "fake__echo", "arguments": {"text": "c"}}))["isError"]
        assert text_of(await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 1, "b": 1}})) == "2"
        # A tool added upstream after pinning is not in this session...
        res = await gw.call("call_tool", {"name": "fake__newcomer"})
        assert res["isError"] is True and "tool not available in this session" in text_of(res)
        # ...but a new session sees both the new echo and the newcomer.
        other = {**IDENT, "X-SB-Session": "sess_other"}
        assert text_of(await gw.call("fake__echo", {"text": "d"}, ident=other)) == "ECHO: D"
        assert text_of(await gw.call("call_tool", {"name": "fake__newcomer"}, ident=other)) == "new"
        await gw.events.drain()
    q = agent_api.ops("quarantine")
    assert q and q[0]["tool"] == "fake__echo" and q[0]["session_id"] == IDENT["X-SB-Session"]


async def test_new_session_pins_fresh_definitions_after_ttl(upstream, harness):
    # Stale-while-revalidate must not reach pin time: once the TTL has passed,
    # a NEW session waits for the refresh and pins what the upstream serves now
    # (else it would pin the replaced def and quarantine it on first use).
    async with harness(make_config(upstream.url)) as gw:
        assert (await gw.call("fake__echo", {"text": "a"}))["isError"] is False
        upstream.change_echo()
        gw.gateway.upstreams["fake"]._fetched_at = 0.0   # the TTL has passed
        other = {**IDENT, "X-SB-Session": "sess_after_ttl"}
        pinned = json.loads(text_of(await gw.call("describe_tool", {"name": "fake__echo"}, ident=other)))
        current = (await gw.gateway.upstreams["fake"].refresh())["echo"][1]   # what upstream serves now
        assert pinned["pin"] == current
        assert text_of(await gw.call("fake__echo", {"text": "d"}, ident=other)) == "ECHO: D"
        # The session pinned before the change is quarantined, not silently moved.
        res = await gw.call("fake__echo", {"text": "b"})
        assert res["isError"] is True and "quarantined" in text_of(res)


async def test_concurrency_limit(upstream, harness):
    async with harness(make_config(upstream.url, max_concurrent=2)) as gw:
        slow1 = asyncio.create_task(gw.call("call_tool", {"name": "fake__slow"}))
        slow2 = asyncio.create_task(gw.call("call_tool", {"name": "fake__slow"}))
        key = ("alice", "wcc-event", IDENT["X-SB-Session"])
        for _ in range(500):
            if gw.gateway.limiter.running(key) == 2:
                break
            await asyncio.sleep(0.01)
        third = await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 1, "b": 1}})
        assert third["isError"] is True and "concurrent" in text_of(third)
        # Another session is not affected.
        other = {**IDENT, "X-SB-Session": "sess_other"}
        ok = await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 1, "b": 1}}, ident=other)
        assert ok["isError"] is False
        upstream.release.set()
        assert text_of(await slow1) == "slow done" and text_of(await slow2) == "slow done"
        assert gw.gateway.limiter.running(key) == 0
        assert (await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 1, "b": 1}}))["isError"] is False


async def test_upstream_timeout(upstream, harness, agent_api):
    async with harness(make_config(upstream.url)) as gw:
        gw.gateway.upstreams["fake"].cfg.timeout_s = 1
        res = await gw.call("call_tool", {"name": "fake__slow"})
        assert res["isError"] is True and "timed out" in text_of(res)
        await gw.events.drain()
    assert any("timed out" in e.get("error", "") for e in agent_api.ops("error"))


async def test_events_failure_never_fails_call(upstream, harness, agent_api):
    agent_api.fail = True
    async with harness(make_config(upstream.url)) as gw:
        res = await gw.call("fake__echo", {"text": "x"})
        assert res["isError"] is False
