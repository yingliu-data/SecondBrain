"""One session identity: (user, tenant, session_id), compared exactly.

Tenant names may contain `_`: tenant `wcc` and tenant `wcc_event` of one
user once shared prefixes (`u/wcc_` of the device-call owner key, the
`wcc_` listing prefix of DirStore) and even directory names (`wcc_event_s1`
= tenant `wcc` + session `event_s1` = tenant `wcc_event` + session `s1`).
No listing, tool result or parked-session load may cross between them.
"""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.auth.middleware import verify
from app.host.sides import DeviceCalls
from app.host.worker import Workers
from app.session.dir_store import DirStore
from app.session.ids import session_key, session_ref
from app.session.session_dir import SessionDir
from host_fakes import FakeRegistry, FakeSandbox, FakeTenant, users_root  # noqa: F401

WCC = FakeTenant(name="wcc", user="u")
WCC_EVENT = FakeTenant(name="wcc_event", user="u")


def _park(session: SessionDir) -> None:
    (session.root / "vm").mkdir(exist_ok=True)
    (session.root / "vm" / "parked.json").write_text(json.dumps({"cseq": 0, "reason": "idle"}))


def _dirs(store: DirStore) -> list[str]:
    return sorted(p.name for p in (store.root / "u").iterdir())


@pytest.fixture
def store(tmp_path, users_root):  # noqa: F811
    store = DirStore(tmp_path / "sessions")
    # wcc_event's legacy session s1 (dir wcc_event_s1, which also reads as
    # tenant wcc + session event_s1) and a new-layout one, both parked
    _park(SessionDir.create(store.root / "u" / "wcc_event_s1", user_id="u", session_id="s1",
                            tenant="wcc_event"))
    _park(store.get_or_create("u", "wcc_event", "s2"))
    store.get_or_create("u", "wcc", "event_s2")                # wcc's own, not parked
    return store


def test_session_keys_are_injective():
    assert session_key("u", "wcc", "event_s1") != session_key("u", "wcc_event", "s1")
    assert session_ref("u", "wcc", "event_s1") != session_ref("u", "wcc_event", "s1")


def test_no_cross_listing(store):
    assert [r["session_id"] for r in store.list_for_tenant("u", "wcc")] == ["event_s2"]
    assert sorted(r["session_id"] for r in store.list_for_tenant("u", "wcc_event")) == ["s1", "s2"]
    assert store.get("u", "wcc", "event_s1") is None           # the legacy dir is wcc_event's


@pytest.mark.asyncio
async def test_load_parked_loads_only_the_tenant_s_sessions_and_makes_no_dir(store):
    workers = Workers(sandbox_factory=lambda: FakeSandbox([]), sessions=store)
    before = _dirs(store)
    try:
        assert await workers.load_parked(WCC, FakeRegistry()) == []
        assert _dirs(store) == before                          # no stray wcc.event_s1
        loaded = await workers.load_parked(WCC_EVENT, FakeRegistry())
        assert sorted(w.session_id for w in loaded) == ["s1", "s2"]
        assert {w.session.root.name for w in loaded} == {"wcc_event_s1", "wcc_event.s2"}
        assert all(w.ref.tenant == "wcc_event" for w in loaded)
        assert _dirs(store) == before
    finally:
        await workers.shutdown(park_s=0)


@pytest.mark.asyncio
async def test_tool_result_never_reaches_another_tenant_s_call(store, monkeypatch):
    """A device call open in wcc_event's session: tenant wcc's POST
    /tool-result is a 409 (and loads none of wcc_event's parked sessions,
    creates no directory); wcc_event's own POST resolves it."""
    from app.routes import chat, tool_result
    dc = DeviceCalls()
    fut = dc.open("call_x", "get_calendar_events", {}, session_ref("u", "wcc_event", "s1"))
    workers = Workers(sandbox_factory=lambda: FakeSandbox([]), sessions=store)
    monkeypatch.setattr(tool_result, "DEVICE_CALLS", dc)
    monkeypatch.setattr(chat, "workers", workers)
    who = {"tenant": WCC}
    app = FastAPI()
    app.include_router(tool_result.router)
    app.dependency_overrides[verify] = lambda: who["tenant"]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    before = _dirs(store)
    try:
        r = await client.post("/api/v1/tool-result", json={"tool_call_id": "call_x", "result": "x"})
        assert r.status_code == 409 and dc.is_open("call_x") and not fut.done()
        assert _dirs(store) == before
        assert workers.get(session_key("u", "wcc", "event_s1")) is None
        who["tenant"] = WCC_EVENT
        r = await client.post("/api/v1/tool-result", json={"tool_call_id": "call_x", "result": "ok"})
        assert r.status_code == 200
        assert await asyncio.wait_for(fut, 1) == "ok"
    finally:
        await client.aclose()
        await workers.shutdown(park_s=0)


def test_listing_reads_only_the_tenant_s_directories_and_what_they_require(store, monkeypatch):
    """list_for_tenant filters directory names by the tenant first (session.json
    of another tenant's directory is never read) and, with `require`, skips a
    directory without that file before reading its identity."""
    import app.session.dir_store as ds
    read = []
    real = ds.read_identity
    monkeypatch.setattr(ds, "read_identity", lambda p: read.append(p.name) or real(p))
    rows = store.list_for_tenant("u", "wcc")
    assert [r["session_id"] for r in rows] == ["event_s2"]
    assert all(n.startswith(("wcc.", "wcc_")) for n in read)
    read.clear()
    assert store.list_for_tenant("u", "wcc", require="vm/parked.json") == []
    assert "wcc.event_s2" not in read      # no parked.json: its session.json is never read
    assert store.list_for_tenant("u", "wcc_event", require="vm/parked.json") == [
        r for r in store.list_for_tenant("u", "wcc_event")
        if (r["root"] / "vm" / "parked.json").exists()]
