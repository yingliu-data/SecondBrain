"""logverify — the end-of-test event log check, a pytest plugin both test
suites load (`pytest_plugins = ["logverify"]` in harness/tests/conftest.py
and agent-api/tests/conftest.py; it sits beside protocol/, not in it,
because protocol is stdlib only).

After every test, `protocol.replay.verify_session` checks every event log
the test wrote under its tmp_path: session / log directories
(`find_sessions`: `<dir>/events/` with runs or segments, the guest's local
buffer included) and every `events.before-resume-N` buffer a log return
moved aside. A problem fails the test (a teardown error naming the log and
the seq). A test that writes a broken log on purpose opts out with
`@pytest.mark.no_log_verify("why")`.
"""
from __future__ import annotations

import pytest

from protocol.replay import find_sessions, verify_session


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_log_verify(reason): skip the end-of-test event log verification "
                   "(the test writes a broken log on purpose)")


def skipped(request) -> bool:
    """The test opted out with @pytest.mark.no_log_verify."""
    return request.node.get_closest_marker("no_log_verify") is not None


def fail_on(problems: list[str]) -> None:
    if problems:
        pytest.fail("event logs written by this test do not verify:\n  "
                    + "\n  ".join(problems[:20]), pytrace=False)


@pytest.fixture(autouse=True)
def verify_event_logs(request):
    """Autouse, so it is set up before (and torn down after) every fixture
    the test requests: the guests / workers are stopped and the logs final
    by then."""
    tmp = request.getfixturevalue("tmp_path") if "tmp_path" in request.fixturenames else None
    yield
    if tmp is None or not tmp.is_dir() or skipped(request):
        return
    fail_on([f"{path.relative_to(tmp)}: {p}"
             for path in find_sessions(tmp, include_before_resume=True)
             for p in verify_session(path)])
