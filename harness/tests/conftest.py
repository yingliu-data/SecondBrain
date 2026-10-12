"""Shared test hooks.

CI log verification (every golden and CI log replays after the run). The
`logverify` plugin (harness/logverify.py, shared with agent-api's suite)
verifies every event log a test wrote under its tmp_path; on top of it,
the events each FakeHost received (the host's copy of the guest's log) are
replayed in memory with fold.replay (validate_log + fold + checkpoints).

A problem fails the test (as a teardown error naming the log and the seq).
A test that writes a broken log on purpose opts out with
`@pytest.mark.no_log_verify("why")`.
"""
from __future__ import annotations

import os

import pytest

# guest.main reads it: the provider's retry backoff in the guest subprocesses
# the tests start (1 s, 2 s, 4 s in production)
os.environ.setdefault("HARNESS_LLM_BACKOFF_S", "0.01")

from protocol.fold import replay
from tests.fakes import coordinator as fake_coordinator
from tests.fakes import fake_host

pytest_plugins = ["logverify"]

_hosts: list = []
_scenarios: list = []


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


def _track_scenarios() -> None:
    """Record every coordinator Scenario a test creates (wraps __init__ once)."""
    cls = fake_coordinator.Scenario
    if getattr(cls, "_view_tracked", False):
        return
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _scenarios.append(self)

    cls.__init__ = __init__
    cls._view_tracked = True


_track_scenarios()


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


@pytest.fixture(autouse=True)
def verify_task_views(request):
    """Autouse: for every coordinator Scenario whose loop has returned, the
    live TaskView equals the one restore() rebuilds from the log (live
    handling and restore share one reducer)."""
    from logverify import skipped
    _scenarios.clear()
    yield
    scenarios = list(_scenarios)
    _scenarios.clear()
    if skipped(request):
        return
    problems = [p for scn in scenarios if scn.thread is not None and not scn.thread.is_alive()
                for p in fake_coordinator.view_problems(scn)]
    if problems:
        pytest.fail("live TaskView differs from the restored one:\n  " + "\n  ".join(problems[:20]),
                    pytrace=False)


@pytest.fixture(autouse=True)
def verify_fake_host_logs(request):
    """Autouse, torn down after every fixture the test requests: the guests
    are stopped by then."""
    from logverify import fail_on, skipped      # the plugin, loaded (and rewritten) by now
    _hosts.clear()
    yield
    hosts = list(_hosts)
    _hosts.clear()
    if skipped(request):
        return
    fail_on([f"FakeHost({host.session_id}) {p}"
             for host in hosts for p in host_log_problems(host)])
