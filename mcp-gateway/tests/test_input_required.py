"""§3.2: the elicitation relay — held upstream call, sb/input_required,
sb/input_response resume, expiry, cancel, concurrency while held."""
import asyncio

from conftest import IDENT, make_config, text_of
from protocol.mcp import INPUT_REQUIRED, INPUT_RESPONSE


def _req(result: dict) -> dict:
    assert result["isError"] is False, result
    return result["_meta"][INPUT_REQUIRED]


async def test_round_trip_promoted(upstream, harness, agent_api):
    cfg = make_config(upstream.url, promoted=["fake__approve"])
    async with harness(cfg) as gw:
        args = {"item": "meetup"}
        first = await gw.call("fake__approve", args)
        req = _req(first)
        assert req["message"] == "Approve publishing meetup?" == text_of(first)
        assert req["schema"]["properties"]["approved"]["type"] == "boolean"
        assert 0 < req["expires_in_s"] <= 30          # remaining upstream budget
        assert len(req["request_state"]) > 20

        resp = {"request_state": req["request_state"], "action": "accept",
                "content": {"approved": True, "note": "ok"}}
        final = await gw.call("fake__approve", args, meta={INPUT_RESPONSE: resp})
        assert final["isError"] is False
        assert text_of(final) == "meetup: approved=True note=ok"
        assert "_meta" not in final

        # The same request_state cannot be used twice.
        again = await gw.call("fake__approve", args, meta={INPUT_RESPONSE: resp})
        assert again["isError"] is True and text_of(again) == "input request expired"
        await gw.events.drain()
    calls = agent_api.ops("call")
    assert len(calls) == 1 and calls[0]["tool"] == "fake__approve" and calls[0]["ok"] is True


async def test_round_trip_via_call_tool_and_two_questions(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        args = {"name": "fake__approve_twice", "arguments": {}}
        r1 = _req(await gw.call("call_tool", args))
        assert r1["message"] == "First?"
        r2 = _req(await gw.call("call_tool", args, meta={INPUT_RESPONSE: {
            "request_state": r1["request_state"], "action": "decline"}}))
        assert r2["message"] == "Second?" and r2["request_state"] != r1["request_state"]
        final = await gw.call("call_tool", args, meta={INPUT_RESPONSE: {
            "request_state": r2["request_state"], "action": "accept", "content": {"approved": False}}})
        assert text_of(final) == "decline/accept"


async def test_cancel(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        args = {"name": "fake__approve", "arguments": {"item": "x"}}
        req = _req(await gw.call("call_tool", args))
        final = await gw.call("call_tool", args, meta={INPUT_RESPONSE: {
            "request_state": req["request_state"], "action": "cancel"}})
        assert final["isError"] is False and text_of(final) == "x: cancel"
    assert upstream.elicit_results == ["cancel"]


async def test_expiry(upstream, harness):
    cfg = make_config(upstream.url)
    cfg.input_expires_s = 1
    async with harness(cfg) as gw:
        args = {"name": "fake__approve", "arguments": {"item": "late"}}
        req = _req(await gw.call("call_tool", args))
        assert req["expires_in_s"] == 1
        for _ in range(300):
            if upstream.elicit_results:
                break
            await asyncio.sleep(0.01)
        assert upstream.elicit_results == ["cancel"]       # the upstream was told
        res = await gw.call("call_tool", args, meta={INPUT_RESPONSE: {
            "request_state": req["request_state"], "action": "accept", "content": {"approved": True}}})
        assert res["isError"] is True and text_of(res) == "input request expired"


async def test_unknown_state_and_wrong_session(upstream, harness):
    async with harness(make_config(upstream.url)) as gw:
        args = {"name": "fake__approve", "arguments": {"item": "x"}}
        res = await gw.call("call_tool", args, meta={INPUT_RESPONSE: {"request_state": "nope", "action": "accept"}})
        assert res["isError"] is True and text_of(res) == "input request expired"

        req = _req(await gw.call("call_tool", args))
        other = {**IDENT, "X-SB-Session": "sess_other"}
        stolen = await gw.call("call_tool", args, ident=other, meta={INPUT_RESPONSE: {
            "request_state": req["request_state"], "action": "accept", "content": {"approved": True}}})
        assert stolen["isError"] is True and text_of(stolen) == "input request expired"
        # Still answerable by its own session.
        final = await gw.call("call_tool", args, meta={INPUT_RESPONSE: {
            "request_state": req["request_state"], "action": "accept", "content": {"approved": True}}})
        assert text_of(final) == "x: approved=True note="


async def test_held_call_does_not_count_toward_concurrency(upstream, harness):
    async with harness(make_config(upstream.url, max_concurrent=1)) as gw:
        args = {"name": "fake__approve", "arguments": {"item": "x"}}
        req = _req(await gw.call("call_tool", args))
        key = ("alice", "wcc-event", IDENT["X-SB-Session"])
        assert gw.gateway.limiter.running(key) == 0
        ok = await gw.call("call_tool", {"name": "fake__add", "arguments": {"a": 1, "b": 2}})
        assert text_of(ok) == "3"
        final = await gw.call("call_tool", args, meta={INPUT_RESPONSE: {
            "request_state": req["request_state"], "action": "accept", "content": {"approved": True}}})
        assert final["isError"] is False
        assert gw.gateway.limiter.running(key) == 0
