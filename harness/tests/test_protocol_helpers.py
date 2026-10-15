"""The small shared helpers in protocol/: ports, limits, fsio, usage, clock,
the bulk log codec, replay's segment reader, the mcp bearer, the version."""
from __future__ import annotations

import dataclasses
import os
import re
import stat
import time

import pytest

from guest.config import Limits
from protocol import bulk, limits, mcp, ports, replay
from protocol.clock import elapsed_ms, now_iso, now_ms
from protocol.fsio import atomic_write, atomic_writer, file_sha256
from protocol.canonical import sha256_hex
from protocol.usage import normalise_usage
from protocol.version import HARNESS_VERSION


def test_ports_and_socket_names():
    assert ports.PORTS == {"control": 5000, "bulk": 5001, "llm": 5010, "gateway": 5011}
    assert ports.socket_name(ports.CONTROL) == "v.sock_5000"


def test_guest_limits_fill_from_default_limits():
    assert dataclasses.asdict(Limits()) == limits.DEFAULT_LIMITS
    a, b = Limits(), Limits()
    a.op_timeout_s["guest"] = 1                 # a copy per instance
    assert b.op_timeout_s == limits.DEFAULT_LIMITS["op_timeout_s"]
    copy = limits.default_limits()
    copy["op_timeout_s"]["mcp"] = 1
    assert limits.DEFAULT_LIMITS["op_timeout_s"]["mcp"] == 300


def test_atomic_write_replaces_whole_files(tmp_path):
    p = tmp_path / "f.json"
    atomic_write(p, b"one")
    assert p.read_bytes() == b"one" and stat.S_IMODE(p.stat().st_mode) == 0o600
    atomic_write(p, b"two", durable=False, mode=0o644)
    assert p.read_bytes() == b"two" and stat.S_IMODE(p.stat().st_mode) == 0o644
    with pytest.raises(RuntimeError):
        with atomic_writer(p) as fh:
            fh.write(b"half")
            raise RuntimeError("boom")
    assert p.read_bytes() == b"two" and os.listdir(tmp_path) == ["f.json"]   # no temp left


def test_file_sha256(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"abc" * 100_000)
    assert file_sha256(p) == sha256_hex(b"abc" * 100_000)
    assert file_sha256(tmp_path / "missing") is None


def test_normalise_usage_has_one_total_rule():
    assert normalise_usage({"prompt_tokens": 3, "completion_tokens": 4}) == {
        "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert normalise_usage({"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 9})[
        "total_tokens"] == 9
    assert normalise_usage(None) == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert normalise_usage({"prompt_tokens": "x", "completion_tokens": None})["total_tokens"] == 0


def test_clock_helpers():
    assert abs(now_ms() - time.time() * 1000) < 1000
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", now_iso())
    t0 = time.monotonic() - 0.01234
    assert isinstance(elapsed_ms(t0), int) and elapsed_ms(t0) >= 12
    assert isinstance(elapsed_ms(t0, 1), float)


def test_log_codec_round_trip_and_gapless_check():
    events = [{"seq": 3, "type": "x", "body": {"b": 1, "a": 2}}, {"seq": 4, "type": "y", "body": {}}]
    payload = bulk.encode_log(events)
    assert payload.splitlines()[0] == b'{"body":{"a":2,"b":1},"seq":3,"type":"x"}'   # canonical
    assert bulk.decode_log(payload, sha256_hex(payload), 2, 4) == events
    assert bulk.decode_log(b"", sha256_hex(b""), 2, 2) == []
    with pytest.raises(bulk.BulkError, match="sha256"):
        bulk.decode_log(payload, "0" * 64, 2, 4)
    with pytest.raises(bulk.BulkError, match="gapless"):
        bulk.decode_log(payload, sha256_hex(payload), 1, 4)
    with pytest.raises(bulk.BulkError, match="last_seq"):
        bulk.decode_log(payload, sha256_hex(payload), 2, 1)


def test_iter_segment_raises_or_reports_a_bad_line(tmp_path):
    seg = tmp_path / replay.segment_name(1)
    seg.write_text('{"seq": 1}\n\nnot json\n{"seq": 2}\n')
    with pytest.raises(replay.ReplayError, match=":3:"):
        list(replay.iter_segment(seg))
    bad = []
    assert list(replay.iter_segment(seg, on_bad=lambda p, n, e: bad.append(n))) == [
        {"seq": 1}, {"seq": 2}]
    assert bad == [3]
    assert replay.BEFORE_RESUME_RE.match(replay.before_resume_name(2))


def test_bearer_helpers():
    assert mcp.bearer_header("") == {} and mcp.bearer_header("t") == {"Authorization": "Bearer t"}
    assert mcp.bearer_ok(None, "") and mcp.bearer_ok("anything", "")
    assert mcp.bearer_ok("Bearer t", "t")
    assert not mcp.bearer_ok("Bearer x", "t") and not mcp.bearer_ok("t", "t")
    assert not mcp.bearer_ok(None, "t")
    assert mcp.gateway_event_identity("u", "t", "s") == {"user": "u", "tenant": "t",
                                                         "session_id": "s"}


def test_one_harness_version():
    from guest import coordinator
    assert coordinator.HARNESS_VERSION == HARNESS_VERSION
