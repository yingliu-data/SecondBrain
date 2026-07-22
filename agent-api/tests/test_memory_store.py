import pytest

from app.user.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


def test_write_list_recall_remove(store):
    store.write(slug="wcc-events", name="WCC events",
                description="user organizes women coding community events",
                type="project", body="Runs the WCC event pipeline.")
    store.write(slug="tz", name="Timezone", description="prefers London timezone",
                type="user", body="Europe/London")

    records = store.list()
    assert {r.slug for r in records} == {"wcc-events", "tz"}

    hits = store.recall("which timezone is preferred")
    assert hits and hits[0].slug == "tz"

    index_text = store.index.read_text()
    assert "wcc-events" in index_text and "tz" in index_text

    assert store.remove("tz") is True
    assert store.remove("tz") is False
    assert "tz" not in store.index.read_text()


def test_overwrite_updates(store):
    store.write(slug="a", name="A", description="first", type="user", body="1")
    store.write(slug="a", name="A", description="second", type="user", body="2")
    records = store.list()
    assert len(records) == 1
    assert records[0].description == "second"


def test_invalid_slug_and_type_rejected(store):
    with pytest.raises(ValueError):
        store.write(slug="../evil", name="x", description="d", type="user", body="b")
    with pytest.raises(ValueError):
        store.write(slug="ok", name="x", description="d", type="banana", body="b")
