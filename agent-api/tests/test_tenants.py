import json

import pytest

from app.tenants import TenantRegistry


def test_loads_tenants_and_synthesizes_default(tenant_registry):
    names = {t.name for t in tenant_registry.all_tenants()}
    assert names == {"wcc-events", "default"}
    default = tenant_registry.get_by_api_key("test-default-key")
    assert default.is_default
    assert default.allowed_skill_names() is None  # unrestricted
    assert default.store_key("abc") == "abc"


def test_missing_file_gives_default_only(tmp_path):
    reg = TenantRegistry(tmp_path / "nope.json", "k")
    assert [t.name for t in reg.all_tenants()] == ["default"]
    assert reg.get_by_api_key("k") is not None
    assert reg.get_by_api_key("wrong") is None


def test_named_tenant_scoping(tenant_registry):
    wcc = tenant_registry.get_by_api_key("wcc-key")
    assert wcc.name == "wcc-events"
    assert wcc.store_key("abc") == "wcc-events:abc"
    assert wcc.allowed_skill_names() == {"web_search"}     # mcp_servers: the gateway's
    assert wcc.max_tools == 2


def test_user_defaults_to_tenant_name(tenant_registry):
    wcc = tenant_registry.get_by_api_key("wcc-key")
    assert wcc.user == "wcc-events"  # no explicit user in conftest doc
    default = tenant_registry.get_by_api_key("test-default-key")
    assert default.user == "default"


def test_explicit_shared_user(tmp_path):
    doc = {"tenants": [
        {"name": "a", "user": "shared", "api_key": "ka"},
        {"name": "b", "user": "shared", "api_key": "kb"},
    ]}
    reg = TenantRegistry(_write(tmp_path, doc), "k")
    assert reg.get_by_api_key("ka").user == "shared"
    assert reg.get_by_api_key("kb").user == "shared"


def test_unsafe_user_rejected(tmp_path):
    doc = {"tenants": [{"name": "a", "user": "../evil", "api_key": "x"}]}
    with pytest.raises(Exception):
        TenantRegistry(_write(tmp_path, doc), "k")


def test_origins_union(tenant_registry):
    assert tenant_registry.all_origins() == ["https://wcc-example-mcp.net"]


def _write(tmp_path, doc):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(doc))
    return p


def test_duplicate_key_rejected(tmp_path):
    doc = {"tenants": [
        {"name": "a", "api_key": "same"},
        {"name": "b", "api_key": "same"},
    ]}
    with pytest.raises(ValueError, match="Duplicate api_key"):
        TenantRegistry(_write(tmp_path, doc), "k")


def test_mcp_servers_accepted_and_ignored(tmp_path):
    """agent-api no longer validates MCP server names (gateway.json does)."""
    doc = {"mcp_servers": {"bad__name": {"url": "http://x/mcp"}},
           "tenants": [{"name": "a", "api_key": "x", "mcp_servers": ["ghost"],
                        "local_skills": []}]}
    reg = TenantRegistry(_write(tmp_path, doc), "k")
    assert reg.get_by_api_key("x").allowed_skill_names() == set()


def test_engine_key_accepted_and_ignored_with_a_warning(tmp_path, caplog):
    doc = {"tenants": [{"name": "a", "api_key": "x", "engine": "legacy"},
                       {"name": "b", "api_key": "y", "engine": "bogus"}]}
    with caplog.at_level("WARNING", logger="tenants"):
        reg = TenantRegistry(_write(tmp_path, doc), "k")
    a = reg.get_by_api_key("x")
    assert not hasattr(a, "engine") and "engine" not in a.workflow_entry()
    assert reg.get_by_api_key("y") is not None
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 2


def test_guest_profile_is_synthesized_and_never_matched_by_key(tmp_path):
    from protocol.allowlist import GUEST_BUILTINS
    from app.auth.guest import ALLOWED_ORIGINS
    reg = TenantRegistry(tmp_path / "none.json", "k")
    g = reg.guest()
    assert reg.get_by_name("guest") is g and g not in reg.all_tenants()
    assert g.auth == "origin" and g.api_key == ""
    assert g.allowed_skill_names() == {"avatar_control"}
    assert set(g.exclude_builtins) == set(GUEST_BUILTINS)
    assert set(g.origins) == ALLOWED_ORIGINS and g.max_tools == 2 and g.answer_wait_s == 0
    assert reg.get_by_api_key("") is None
    assert "{current_time}" in g.system_prompt
    # not in the key tenants' CORS origins (main.py adds the guest's own)
    assert reg.all_origins() == []


def test_guest_profile_from_tenants_json(tmp_path):
    doc = {"tenants": [{"name": "guest", "auth": "origin", "origins": ["https://x.example"],
                        "local_skills": ["avatar_control"], "max_tools": 4}]}
    reg = TenantRegistry(_write(tmp_path, doc), "k")
    assert reg.guest().origins == ["https://x.example"] and reg.guest().max_tools == 4


def test_key_tenant_needs_a_key_and_builtins_are_checked():
    from app.tenants import Tenant
    with pytest.raises(ValueError, match="api_key is required"):
        Tenant(name="a")
    with pytest.raises(ValueError, match="not guest built-ins"):
        Tenant(name="a", api_key="k", exclude_builtins=["nope"])
