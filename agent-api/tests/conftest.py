import os

# Must be set before any app.* import — config.py reads it at import time.
os.environ.setdefault("API_SECRET_KEY", "test-default-key")
# No gateway unless a test points a Worker / skill at a fake one.
os.environ.setdefault("MCP_GATEWAY_URL", "")

import json
import re

import pytest

from protocol.replay import find_sessions, verify_session

from app.tenants import TenantRegistry


TENANTS_DOC = {
    "mcp_servers": {
        "wcc": {
            "url": "http://host.docker.internal:8765/mcp",
            "auth_token": "srv-token",
            "timeout_s": 60,
            "allow_private": True,
        }
    },
    "tenants": [
        {
            "name": "wcc-events",
            "api_key": "wcc-key",
            "origins": ["https://wcc-example-mcp.net"],
            "system_prompt": "WCC prompt. Current time: {current_time}",
            "local_skills": ["web_search"],
            "mcp_servers": ["wcc"],
            "max_tools": 2,
            "max_tokens": 1024,
        }
    ],
}


@pytest.fixture
def tenants_file(tmp_path):
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(TENANTS_DOC))
    return path


@pytest.fixture
def tenant_registry(tenants_file):
    return TenantRegistry(tenants_file, "test-default-key")


# --------------------------------------------------------------------------- #
# CI log verification (M6-CONTRACTS section 1; the same hook as
# harness/tests/conftest.py): after every test, `protocol.replay` verifies
# every session / log directory the test wrote under its tmp_path, and every
# `events.before-resume-N` buffer a log return moved aside. A problem fails
# the test (a teardown error naming the log and the seq). A test that writes
# a broken log on purpose opts out with `@pytest.mark.no_log_verify("why")`.

_BEFORE_RESUME = re.compile(r"^events\.before-resume-\d+$")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_log_verify(reason): skip the end-of-test event log verification "
                   "(the test writes a broken log on purpose)")


def produced_logs(root) -> list:
    """Every event log directory under root: session / log dirs and the
    buffers a log return moved aside."""
    found = list(find_sessions(root))
    found.extend(p for p in sorted(root.rglob("events.before-resume-*"))
                 if p.is_dir() and _BEFORE_RESUME.match(p.name))
    return found


@pytest.fixture(autouse=True)
def verify_event_logs(request):
    """Autouse, so it is torn down after every fixture the test requests:
    the workers are closed and the logs final by then."""
    tmp = request.getfixturevalue("tmp_path") if "tmp_path" in request.fixturenames else None
    yield
    if tmp is None or not tmp.is_dir() or request.node.get_closest_marker("no_log_verify"):
        return
    problems = [f"{path.relative_to(tmp)}: {p}"
                for path in produced_logs(tmp) for p in verify_session(path)]
    if problems:
        pytest.fail("event logs written by this test do not verify:\n  "
                    + "\n  ".join(problems[:20]), pytrace=False)
