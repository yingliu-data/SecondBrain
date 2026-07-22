import json

import pytest

from app.tenants import TenantRegistry


def test_loads_tenants_and_synthesizes_default(tenant_registry):
    names = {t.name for t in tenant_registry.all_tenants()}
    assert names == {"wcc-events", "default"}
    default = tenant_registry.get_by_api_key("test-default-key")
    assert default.is_default
    assert default.allowed_skill_names() is None  # unrestricted
    assert default.session_key("abc") == "abc"


def test_missing_file_gives_default_only(tmp_path):
    reg = TenantRegistry(tmp_path / "nope.json", "k")
    assert [t.name for t in reg.all_tenants()] == ["default"]
    assert reg.get_by_api_key("k") is not None
    assert reg.get_by_api_key("wrong") is None


def test_named_tenant_scoping(tenant_registry):
    wcc = tenant_registry.get_by_api_key("wcc-key")
    assert wcc.name == "wcc-events"
    assert wcc.session_key("abc") == "wcc-events:abc"
    assert wcc.allowed_skill_names() == {"web_search", "mcp_wcc"}
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


def test_unknown_mcp_server_rejected(tmp_path):
    doc = {"tenants": [{"name": "a", "api_key": "x", "mcp_servers": ["ghost"]}]}
    with pytest.raises(ValueError, match="unknown MCP server"):
        TenantRegistry(_write(tmp_path, doc), "k")


def test_server_name_with_separator_rejected(tmp_path):
    doc = {"mcp_servers": {"bad__name": {"url": "http://x/mcp"}}, "tenants": []}
    with pytest.raises(Exception):
        TenantRegistry(_write(tmp_path, doc), "k")
