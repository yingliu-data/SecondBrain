import asyncio

import pytest

from app.session.dir_store import DirStore
from app.session.queue import SessionQueue
from app.user.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return DirStore(tmp_path / "sessions")


def test_nested_layout(store, tmp_path):
    session = store.get_or_create("wcc", "wcc-event", "s1")
    assert session.root == tmp_path / "sessions" / "wcc" / "wcc-event_s1"
    assert session.read_meta()["tenant"] == "wcc-event"


def test_tenant_isolation_same_user(store):
    store.get_or_create("wcc", "wcc-event", "s1")
    store.get_or_create("wcc", "wcc-analytic", "s2")
    events = store.list_for_tenant("wcc", "wcc-event")
    assert [r["session_id"] for r in events] == ["s1"]
    analytic = store.list_for_tenant("wcc", "wcc-analytic")
    assert [r["session_id"] for r in analytic] == ["s2"]
    assert len(store.list_for_user("wcc")) == 2


def test_user_isolation(store):
    store.get_or_create("alice", "t1", "s1")
    store.get_or_create("bob", "t1", "s1")
    assert len(store.list_for_tenant("alice", "t1")) == 1
    assert len(store.list_for_tenant("bob", "t1")) == 1
    assert store.list_for_tenant("carol", "t1") == []


def test_delete(store):
    store.get_or_create("u", "t", "s1")
    assert store.delete("u", "t", "s1") is True
    assert store.delete("u", "t", "s1") is False
    assert store.get("u", "t", "s1") is None


def test_unsafe_ids_rejected(store):
    with pytest.raises(ValueError):
        store.get_or_create("../evil", "t", "s1")
    with pytest.raises(ValueError):
        store.get_or_create("u", "t", "../../etc")


def test_user_memory_shared_across_tenants(tmp_path):
    """User-scope memory lives per user, not per tenant — the same user's
    two tenants read one store; a different user reads another."""
    users_root = tmp_path / "users"
    wcc_memory = MemoryStore(users_root / "wcc" / "memory")
    wcc_memory.write(slug="tz", name="Timezone", description="prefers London time",
                     type="user", body="Europe/London")
    # Same path derivation any tenant of user "wcc" would use:
    assert MemoryStore(users_root / "wcc" / "memory").recall("timezone")
    assert MemoryStore(users_root / "other" / "memory").recall("timezone") == []


@pytest.mark.asyncio
async def test_queue_serializes_same_key():
    q = SessionQueue()
    order = []

    async def job(tag, delay):
        async with q.run("same"):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    await asyncio.gather(job("a", 0.05), job("b", 0))
    assert order == ["a-in", "a-out", "b-in", "b-out"]
    assert q._locks == {} and q._refs == {}  # evicted when idle


@pytest.mark.asyncio
async def test_queue_parallel_across_keys():
    q = SessionQueue()
    running = set()
    overlap = []

    async def job(key):
        async with q.run(key):
            running.add(key)
            await asyncio.sleep(0.05)
            overlap.append(len(running))
            running.discard(key)

    await asyncio.gather(job("k1"), job("k2"))
    assert max(overlap) == 2  # both held their locks simultaneously
