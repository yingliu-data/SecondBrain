"""Policy, pins, schema, ssrf, migrate_config."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import types
from protocol.mcp import split_namespaced

from app import ssrf
from app.config import GatewayConfig, UpstreamConfig
from app.migrate_config import migrate
from app.pins import Identity, PinStore, pin_of
from app.policy import ConcurrencyLimiter, Policy
from app.schema import defn_to_openai, tool_defn
from app.upstreams import Upstream, UpstreamUnavailable

ROOT = Path(__file__).resolve().parents[1]


def _cfg(**tenant):
    return GatewayConfig.from_dict({
        "upstreams": {"a": {"url": "http://a/mcp"}, "b": {"url": "http://b/mcp"}},
        "tenants": {"t": {"upstreams": ["a", "missing"], **tenant}},
    })


def test_policy_default_deny_and_globs():
    p = Policy(_cfg(allow=["a__get_*", "a__list"], deny=["a__get_secret*"], promoted=["a__list", "b__x", "a__nope"]))
    assert p.allowed("t", "a__get_event")
    assert p.allowed("t", "a__list")
    assert not p.allowed("t", "a__get_secret_key")       # deny wins over allow
    assert not p.allowed("t", "a__delete")               # not in allow
    assert not p.allowed("t", "b__x")                    # upstream not listed for the tenant
    assert not p.allowed("t", "missing__x")              # upstream not configured
    assert not p.allowed("other", "a__list")             # tenant absent
    assert not p.allowed("t", "a")                       # not namespaced
    assert p.upstreams_for("t") == ["a"]
    assert p.promoted("t") == ["a__list"]
    assert p.max_concurrent("t") == 2


def test_policy_deny_wins_regardless_of_order():
    p = Policy(_cfg(allow=["a__x"], deny=["*"]))
    assert not p.allowed("t", "a__x")


def test_limiter():
    lim = ConcurrencyLimiter()
    assert lim.try_acquire("k", 1) and not lim.try_acquire("k", 1)
    lim.release("k")
    assert lim.running("k") == 0 and lim.try_acquire("k", 1)


def test_split_namespaced():
    assert split_namespaced("wcc__draft__event") == ("wcc", "draft__event")


def test_pin_is_stable_and_conversion_does_not_mutate():
    tool = types.Tool(name="t", description="d",
                      inputSchema={"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                                   "properties": {"x": {"type": "string"}}},
                      annotations=types.ToolAnnotations(readOnlyHint=True))
    before = pin_of(tool_defn(tool))
    openai = defn_to_openai("up__t", tool_defn(tool))
    assert "$schema" not in openai["function"]["parameters"]
    assert "$schema" in tool.inputSchema
    assert pin_of(tool_defn(tool)) == before
    defn = tool_defn(tool)
    assert defn["annotations"] == {"readOnlyHint": True}
    assert set(defn) == {"name", "description", "inputSchema", "annotations"}


def test_pin_store_atomic_round_trip(tmp_path):
    ident = Identity("u", "t", "s1")
    store = PinStore(tmp_path)
    sp = store.create(ident, {"a__x": {"pin": "p", "defn": {"name": "x"}}}, [])
    sp.quarantined.add("a__x")
    store.save(sp)
    files = list((tmp_path / "pins" / "u" / "t").iterdir())
    assert [f.name for f in files] == ["s1.json"]          # no temp files left behind
    fresh = PinStore(tmp_path).get(ident)
    assert fresh.tools == sp.tools and fresh.quarantined == {"a__x"}
    assert PinStore(tmp_path).get(Identity("u", "t", "s2")) is None


def test_ssrf():
    ssrf.validate_mcp_url(UpstreamConfig(name="x", url="http://127.0.0.1/mcp", allow_private=True), [])
    ssrf.validate_mcp_url(UpstreamConfig(name="x", url="http://host.docker.internal/mcp"), ["host.docker.internal"])
    with pytest.raises(ValueError):
        ssrf.validate_mcp_url(UpstreamConfig(name="x", url="http://127.0.0.1/mcp"), [])
    with pytest.raises(ValueError):
        ssrf.validate_mcp_url(UpstreamConfig(name="x", url="file:///etc/passwd", allow_private=True), [])


def test_example_config_loads():
    cfg = GatewayConfig.load(ROOT / "gateway.json.example")
    assert set(cfg.upstreams) == {"wcc-events", "wcc-mentorship", "wcc-analytic"}
    assert Policy(cfg).upstreams_for("default") == ["wcc-events", "wcc-mentorship", "wcc-analytic"]
    assert set(cfg.tenants) == {"default"}


def test_upstream_name_validation():
    with pytest.raises(ValueError):
        UpstreamConfig(name="a__b", url="http://x")


TENANTS = {
    "_comment": "x",
    "mcp_servers": {
        "wcc-events": {"url": "http://h:8765/mcp/events", "auth_token": "t1", "timeout_s": 2700,
                       "tool_defs_ttl_s": 300, "allow_private": True},
        "wcc-analytic": {"url": "http://h:8765/mcp/analytic", "auth_token": "t3", "timeout_s": 1200},
    },
    "tenants": [
        {"name": "wcc-event", "api_key": "k1", "mcp_servers": ["wcc-events"], "promoted": ["wcc-events__draft_event"]},
        {"name": "wcc-analytic", "api_key": "k2", "mcp_servers": ["wcc-analytic"]},
        {"name": "web-only", "api_key": "k3", "local_skills": ["web_search"]},
    ],
}


def test_migrate_synthesized_default():
    out = migrate(TENANTS)
    assert out["upstreams"]["wcc-events"] == TENANTS["mcp_servers"]["wcc-events"]
    assert out["tenants"]["wcc-event"] == {"upstreams": ["wcc-events"], "allow": ["*"], "deny": [],
                                           "promoted": ["wcc-events__draft_event"], "max_concurrent": 2}
    assert out["tenants"]["wcc-analytic"]["promoted"] == []
    assert "web-only" not in out["tenants"]
    assert out["tenants"]["default"]["upstreams"] == ["wcc-events", "wcc-analytic"]
    GatewayConfig.from_dict(out)   # loads


def test_migrate_explicit_default():
    data = json.loads(json.dumps(TENANTS))
    data["tenants"].append({"name": "me", "api_key": "secret", "is_default": True, "engine": "harness"})
    out = migrate(data)
    assert out["tenants"]["me"]["upstreams"] == ["wcc-events", "wcc-analytic"]
    assert "default" not in out["tenants"]
    # An entry whose api_key equals API_SECRET_KEY is the default too.
    out = migrate(TENANTS, api_secret_key="k3")
    assert out["tenants"]["web-only"]["upstreams"] == ["wcc-events", "wcc-analytic"]
    assert "default" not in out["tenants"]


def test_migrate_cli_on_example(tmp_path):
    example = ROOT.parent / "agent-api" / "tenants.json.example"
    r = subprocess.run([sys.executable, "-m", "app.migrate_config", str(example)], cwd=ROOT,
                       capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"})
    out = json.loads(r.stdout)
    assert set(out["upstreams"]) == {"wcc-events", "wcc-mentorship", "wcc-analytic"}
    assert set(out["tenants"]) == {"default"}
    assert out["tenants"]["default"]["upstreams"] == ["wcc-events", "wcc-mentorship", "wcc-analytic"]


async def test_upstream_failure_is_cached():
    up = Upstream(UpstreamConfig(name="x", url="http://127.0.0.1:9/mcp", allow_private=True), [], hold_s=0)
    try:
        with pytest.raises(Exception) as first:
            await up.tools()
        assert not isinstance(first.value, UpstreamUnavailable)
        with pytest.raises(UpstreamUnavailable):        # no reconnect inside the backoff
            await up.tools()
    finally:
        await up.aclose()
