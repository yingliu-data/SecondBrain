"""Shared test hooks.

CI log verification (every golden and CI log replays after the run).
Every test is followed by `protocol.replay` over every event log it
produced:

  - the events each FakeHost received (the host's copy of the guest's log),
    replayed in memory with fold.replay (validate_log + fold + checkpoints);
  - every session / log directory under the test's tmp_path
    (`protocol.replay.find_sessions`: `<dir>/events/` with runs or segments,
    the guest's local buffer included) and every `events.before-resume-N`
    buffer a log return moved aside, with `verify_session`.

A problem fails the test (as a teardown error naming the log and the seq).
A test that writes a broken log on purpose opts out with
`@pytest.mark.no_log_verify("why")`.
"""
from __future__ import annotations

import re

import pytest

from protocol.fold import replay
from protocol.replay import find_sessions, verify_session
from tests.fakes import fake_host

_BEFORE_RESUME = re.compile(r"^events\.before-resume-\d+$")
_hosts: list = []


def _track_hosts() -> None:
    """Record every FakeHost a test creates (wraps __init__ once)."""
    cls = fake_host.FakeHost
    if getattr(cls, "_log_tracked", False):
        return
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _hosts.append(self)

    cls.__init__ = __init__
    cls._log_tracked = True


_track_hosts()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_log_verify(reason): skip the end-of-test event log verification "
                   "(the test writes a broken log on purpose)")


def host_log_problems(host) -> list[str]:
    """fold.replay over the events a FakeHost received, split into runs at
    each session_start (a fresh guest starts again at seq 1)."""
    runs: list[list[dict]] = []
    for ev in list(host.events):
        if ev.get("type") == "session_start" or not runs:
            runs.append([])
        runs[-1].append(ev)
    problems = []
    for i, events in enumerate(runs, 1):
        _state, errors = replay(events)
        problems.extend(f"run {i}: {e}" for e in errors)
    return problems


def produced_logs(root) -> list:
    """Every event log directory under root: session / log dirs and the
    buffers a log return moved aside."""
    found = list(find_sessions(root))
    found.extend(p for p in sorted(root.rglob("events.before-resume-*"))
                 if p.is_dir() and _BEFORE_RESUME.match(p.name))
    return found


@pytest.fixture(autouse=True)
def verify_event_logs(request):
    """Autouse, so it is set up before (and torn down after) every fixture
    the test requests: the guests are stopped and the logs final by then."""
    tmp = request.getfixturevalue("tmp_path") if "tmp_path" in request.fixturenames else None
    _hosts.clear()
    yield
    hosts = list(_hosts)
    _hosts.clear()
    if request.node.get_closest_marker("no_log_verify"):
        return
    problems = []
    for host in hosts:
        problems.extend(f"FakeHost({host.session_id}) {p}" for p in host_log_problems(host))
    if tmp is not None and tmp.is_dir():
        for path in produced_logs(tmp):
            problems.extend(f"{path.relative_to(tmp)}: {p}" for p in verify_session(path))
    if problems:
        pytest.fail("event logs written by this test do not verify:\n  "
                    + "\n  ".join(problems[:20]), pytrace=False)
