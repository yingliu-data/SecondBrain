"""make_goldens — deterministic generator of the three golden logs.

Run:  cd harness && python3 tests/golden/make_goldens.py
Writes text_answer.jsonl, device_tool_late_duplicate.jsonl and
mcp_tool_via_gateway.jsonl next to this file. Standard library only; no clock,
no randomness: ts values are fixed strings, ids come from canonical.derive_id,
call_ids are fixed strings (vLLM's, verbatim), and every checkpoint's
state_hash is computed by protocol.fold on the events before it.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from protocol.allowlist import effective_tools  # noqa: E402
from protocol.canonical import derive_id, sha256_hex  # noqa: E402
from tests.logbuilder import Log  # noqa: E402

USER = "golden-user"
TENANT = "default"
MODEL = "Qwen/Qwen3-14B"
PARAMS = {"temperature": 0, "max_tokens": 1024}


def _ts(seq: int) -> str:
    return f"2026-10-07T12:00:00.{seq:03d}Z"


def _log(session_id: str, extra_tools: list) -> Log:
    """A log whose session_start offers the guest built-ins plus extra_tools."""
    log = Log(session_id, tools=effective_tools({"tools": extra_tools}), ts=_ts)
    log.start(
        user=USER, tenant=TENANT,
        workflow_run_id=f"{USER}/{TENANT}/{session_id}",
        config_hash=sha256_hex("config:" + session_id),
        system_hash=sha256_hex("system:" + session_id),
        image_version="golden-0",
    )
    return log


def _id(log: Log, kind: str) -> str:
    """The derived id minted at the next seq."""
    return derive_id(USER, TENANT, log.session_id, log.next_seq, kind)


def _turn(log: Log) -> str:
    """turn_start covering everything so far; returns the turn_id."""
    turn_id = _id(log, "turn")
    log.turn(
        turn_id, model=MODEL, params=PARAMS,
        request_hash=sha256_hex(f"request:{log.session_id}:{turn_id}"),
        preamble=sha256_hex(f"preamble:{log.session_id}:{turn_id}"),
    )
    return turn_id


def _usage(p: int, c: int) -> dict:
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


# ---------------------------------------------------------------- goldens
def text_answer() -> list[dict]:
    log = _log("golden-text-answer", [])
    log.add("input", {"input_id": "in-0001", "kind": "task",
                      "text": "What is 2 + 2?", "dedupe": "new"})
    log.add("clock", {"kind": "turn_clock", "at": 1_790_000_000_000})
    turn_id = _turn(log)
    log.add("llm_response", {
        "turn_id": turn_id,
        "message": {"role": "assistant", "content": "2 + 2 = 4."},
        "finish_reason": "stop", "usage": _usage(120, 9),
        "response_id": "chatcmpl-golden-1",
    })
    log.add("settle", {"input_id": "in-0001", "outcome": "done", "answer": "2 + 2 = 4.",
                       "usage": _usage(120, 9), "manifest": {}})
    log.checkpoint()
    return log.events


def device_tool_late_duplicate() -> list[dict]:
    log = _log("golden-device-late", [{"name": "calendar", "side": "device", "pin": None}])
    log.add("input", {"input_id": "in-0002", "kind": "task",
                      "text": "What is on my calendar today?", "dedupe": "new"})
    log.add("clock", {"kind": "turn_clock", "at": 1_790_000_100_000})
    t1 = _turn(log)
    call_id = "call_golden_dev_01"
    args = {"action": "list", "day": "2026-10-07"}
    log.add("llm_response", {
        "turn_id": t1,
        "message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "calendar", "arguments": json.dumps(args, separators=(",", ":"))},
        }]},
        "finish_reason": "tool_calls", "usage": _usage(140, 31),
        "response_id": "chatcmpl-golden-2a",
    })
    log.add("guard_decision", {"ref": call_id, "decision": "allow",
                               "reason": "allowlist", "policy_id": "pass-through"})
    log.add("tool_call", {"turn_id": t1, "call_id": call_id, "name": "calendar",
                          "side": "device", "args": args, "state": "RUNNING"})
    log.add("op_state", {"call_id": call_id, "state": "running", "side_ref": "sse:1"})
    log.add("op_state", {"call_id": call_id, "state": "done", "duration_ms": 1850})
    log.add("tool_result", {"call_id": call_id,
                            "content": "[{\"title\":\"Standup\",\"start\":\"09:30\"}]"})
    log.add("wake", {"reason": "tool_result", "result_ids": [call_id], "input_ids": []})
    log.add("clock", {"kind": "turn_clock", "at": 1_790_000_102_000})
    t2 = _turn(log)
    log.add("llm_response", {
        "turn_id": t2,
        "message": {"role": "assistant", "content": "You have one event today: Standup at 09:30."},
        "finish_reason": "stop", "usage": _usage(210, 14),
        "response_id": "chatcmpl-golden-2b",
    })
    log.add("settle", {"input_id": "in-0002", "outcome": "done",
                       "answer": "You have one event today: Standup at 09:30.",
                       "usage": _usage(350, 45), "manifest": {}})
    # the phone re-POSTs the same input after the answer: recorded, not acted on
    log.add("input", {"input_id": "in-0002", "kind": "task",
                      "text": "What is on my calendar today?", "dedupe": "duplicate"})
    # a second device result for the terminal call: recorded late, ignored
    log.add("tool_result", {"call_id": call_id,
                            "content": "[{\"title\":\"Standup\",\"start\":\"09:30\"}]",
                            "late": True})
    log.checkpoint()
    return log.events


def mcp_tool_via_gateway() -> list[dict]:
    tool = "wcc-events__draft_event"
    pin = sha256_hex("mcp-tool-definition:" + tool)
    log = _log("golden-mcp-gateway", [{"name": tool, "side": "mcp", "pin": pin}])
    log.add("input", {"input_id": "in-0003", "kind": "task",
                      "text": "Draft the October meetup event.", "dedupe": "new"})
    log.add("clock", {"kind": "turn_clock", "at": 1_790_000_200_000})
    t1 = _turn(log)
    call_id = "call_golden_mcp_01"
    args = {"title": "October meetup", "date": "2026-10-21"}
    log.add("llm_response", {
        "turn_id": t1,
        "message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args, separators=(",", ":"))},
        }]},
        "finish_reason": "tool_calls", "usage": _usage(160, 40),
        "response_id": "chatcmpl-golden-3a",
    })
    log.add("guard_decision", {"ref": call_id, "decision": "allow",
                               "reason": "allowlist", "policy_id": "pass-through"})
    log.add("tool_call", {"turn_id": t1, "call_id": call_id, "name": tool,
                          "side": "mcp", "args": args, "state": "RUNNING"})
    log.add("op_state", {"call_id": call_id, "state": "running", "side_ref": "gateway:wcc-events"})
    log.add("op_state", {"call_id": call_id, "state": "done", "duration_ms": 4200})
    full = "# October meetup\n" + ("Agenda line.\n" * 200)
    spill_path = f"spill/{call_id}.txt"
    log.add("bulk", {"kind": "spill", "path": spill_path,
                     "sha256": sha256_hex(full), "bytes": len(full.encode("utf-8"))})
    log.add("tool_result", {"call_id": call_id, "content": full[:120] + "\n[truncated]",
                            "spill_ref": spill_path})
    log.add("wake", {"reason": "tool_result", "result_ids": [call_id], "input_ids": []})
    log.add("clock", {"kind": "turn_clock", "at": 1_790_000_206_000})
    t2 = _turn(log)
    log.add("llm_response", {
        "turn_id": t2,
        "message": {"role": "assistant", "content": "Drafted the October meetup for 2026-10-21."},
        "finish_reason": "stop", "usage": _usage(260, 12),
        "response_id": "chatcmpl-golden-3b",
    })
    log.add("settle", {"input_id": "in-0003", "outcome": "done",
                       "answer": "Drafted the October meetup for 2026-10-21.",
                       "usage": _usage(420, 52), "manifest": {}})
    log.checkpoint()
    return log.events


GOLDENS = {
    "text_answer": text_answer,
    "device_tool_late_duplicate": device_tool_late_duplicate,
    "mcp_tool_via_gateway": mcp_tool_via_gateway,
}


def build_all() -> dict[str, list[dict]]:
    return {name: fn() for name, fn in GOLDENS.items()}


def render(events: list[dict]) -> str:
    return "".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in events)


def write_all(directory: str = HERE) -> list[str]:
    paths = []
    for name, events in build_all().items():
        path = os.path.join(directory, name + ".jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render(events))
        paths.append(path)
    return paths


if __name__ == "__main__":
    for p in write_all():
        print(p)
