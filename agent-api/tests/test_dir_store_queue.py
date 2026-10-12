import asyncio

import pytest

from app.session.dir_store import DirStore
from app.session.session_dir import SessionDir
from app.user.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return DirStore(tmp_path / "sessions")


def test_nested_layout(store, tmp_path):
    session = store.get_or_create("wcc", "wcc-event", "s1")
    assert session.root == tmp_path / "sessions" / "wcc" / "wcc-event.s1"
    assert session.read_meta()["tenant"] == "wcc-event"


def _legacy(store, user, tenant, sid):
    """A session directory in the pre-`.` layout {tenant}_{session_id}."""
    return SessionDir.create(store.root / user / f"{tenant}_{sid}", user_id=user,
                             session_id=sid, tenant=tenant)


def test_tenant_names_with_underscores_never_collide(store):
    """Tenant `wcc` + session `event_s1` and tenant `wcc_event` + session
    `s1` had one directory name (`wcc_event_s1`); now they are two sessions
    and every listing compares session.json's tenant exactly."""
    a = store.get_or_create("u", "wcc", "event_s1")
    b = store.get_or_create("u", "wcc_event", "s1")
    assert a.root != b.root and {a.root.name, b.root.name} == {"wcc.event_s1", "wcc_event.s1"}
    assert [r["session_id"] for r in store.list_for_tenant("u", "wcc")] == ["event_s1"]
    assert [r["session_id"] for r in store.list_for_tenant("u", "wcc_event")] == ["s1"]
    assert store.delete("u", "wcc", "event_s1") is True
    assert store.get("u", "wcc_event", "s1") is not None
    assert store.list_for_tenant("u", "wcc") == []


def test_legacy_directories_are_read_by_their_session_json_only(store):
    legacy = _legacy(store, "u", "wcc_event", "s1")          # dir u/wcc_event_s1
    _legacy(store, "u", "wcc", "x")                          # dir u/wcc_x
    # the legacy name is also tenant `wcc` + session `event_s1`: not that session
    assert store.get("u", "wcc", "event_s1") is None
    assert store.path_for("u", "wcc", "event_s1").name == "wcc.event_s1"
    assert store.get("u", "wcc_event", "s1").root == legacy.root
    assert [r["session_id"] for r in store.list_for_tenant("u", "wcc")] == ["x"]
    assert [r["session_id"] for r in store.list_for_tenant("u", "wcc_event")] == ["s1"]
    assert store.get_or_create("u", "wcc_event", "s1").root == legacy.root   # kept in place
    new = store.get_or_create("u", "wcc", "event_s1")        # a new, separate directory
    assert new.root.name == "wcc.event_s1" and legacy.read_meta()["tenant"] == "wcc_event"
    assert sorted(r["tenant"] for r in store.list_for_user("u")) == ["wcc", "wcc", "wcc_event"]
    assert store.delete("u", "wcc", "event_s1") and (legacy.root / "session.json").exists()


def test_a_directory_not_named_for_its_session_is_not_listed(store):
    """A copied directory (name and session.json disagree) is no session of
    the tenant its session.json names."""
    import shutil
    a = store.get_or_create("u", "t", "s1")
    shutil.copytree(a.root, store.root / "u" / "other.s9")
    assert [r["session_id"] for r in store.list_for_tenant("u", "t")] == ["s1"]
    assert store.list_for_tenant("u", "other") == []


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
