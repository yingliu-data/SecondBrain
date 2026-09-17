#!/usr/bin/env python3
"""PW-2 / PW-3 — measure the numbers SYSTEM_DESIGN.md currently guesses.

WHY: design section 8.1 presents a seven-row latency budget summing to ~3.7s and
calls the total "the single most important number in the document". Every row in
it is an estimate. Section 8.3 caps tools-in-context at 10, citing Qwen3-14B
accuracy degradation, also unmeasured. Phases 6 and 7 of WORK_PLAN.md are built
on both claims.

This script replaces the guesses with measurements, and is the stated go/no-go
gate on Phase 7. If a non-thinking tool-decision round comes back materially
over ~1500ms, the three-second voice product is not reachable with this model
and the correct response is a smaller model or a narrower scope -- NOT a faster
harness. Find that out before building Phases 6-7, not after.

It talks to the OpenAI-compatible vLLM endpoint directly. It imports nothing
from app/, so it runs anywhere with network reach to the server.

    # on the GPU box, or anywhere that can reach it
    uv run python scripts/measure_latency.py --url http://localhost:8080
    uv run python scripts/measure_latency.py --url http://localhost:8080 --repeats 20 --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

try:
    import httpx
except ImportError:
    sys.exit("httpx missing. Run with: uv run python scripts/measure_latency.py")


# ---------------------------------------------------------------- tool corpus
# Flat argument schemas throughout: design section 4.2 notes Qwen3-14B handles
# flat far better than nested, so measuring with nested schemas would understate
# the model unfairly.
def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


CORE_TOOLS = [
    _tool("get_departures", "Get live train departure times between two UK stations.",
          {"origin": {"type": "string", "description": "Origin station name or CRS code"},
           "destination": {"type": "string", "description": "Destination station name or CRS code"}},
          ["origin"]),
    _tool("get_weather", "Get current weather or forecast for a location.",
          {"location": {"type": "string", "description": "Place name"},
           "days": {"type": "integer", "description": "Forecast days ahead, 0 for current"}},
          ["location"]),
    _tool("search_places", "Find restaurants, bars or venues in an area.",
          {"query": {"type": "string"}, "area": {"type": "string"}},
          ["query"]),
    _tool("create_reminder", "Create a reminder on the user's phone.",
          {"title": {"type": "string"}, "due": {"type": "string", "description": "ISO 8601"}},
          ["title"]),
    _tool("get_calendar_events", "List the user's calendar events for a date.",
          {"date": {"type": "string", "description": "YYYY-MM-DD"}}, ["date"]),
    _tool("remember_fact", "Store a durable fact about the user.",
          {"fact": {"type": "string"}}, ["fact"]),
]

# Filler that is plausible but never correct for the probes below. This is the
# realistic case: a large catalogue of things that are almost relevant.
FILLER_TOOLS = [
    _tool(f"{verb}_{noun}", f"{verb.capitalize()} a {noun} record in the user's workspace.",
          {"identifier": {"type": "string"}, "value": {"type": "string"}}, ["identifier"])
    for verb, noun in [
        ("list", "issue"), ("create", "issue"), ("search", "email"), ("send", "email"),
        ("read", "clipboard"), ("set", "pose"), ("move", "joints"), ("animate", "sequence"),
        ("list", "repository"), ("create", "pullrequest"), ("search", "web"), ("fetch", "url"),
        ("list", "contact"), ("update", "profile"),
    ]
]

# (prompt, expected tool). Deliberately phrased the way someone speaks to a
# voice assistant, not the way a benchmark is written.
PROBES = [
    ("When's the next train from Woking to Waterloo?", "get_departures"),
    ("What's the weather like in Brussels tomorrow?", "get_weather"),
    ("Find me somewhere to eat near Shoreditch", "search_places"),
    ("Remind me to call the dentist at four", "create_reminder"),
    ("What's on my calendar today?", "get_calendar_events"),
]

SYSTEM = ("You are a personal assistant. Use the function calling capability when a tool "
          "fits the request. Do not write tool calls as text. Be concise.")


def _payload(model, prompt, tools, thinking, stream, max_tokens=256):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    if not thinking:
        # Matches what app/agent/loop.py sends on every call today.
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def measure_round(client, model, prompt, expected, tools, thinking):
    """One non-streaming tool-decision round. Returns (ms, chose_right, finish)."""
    t0 = time.perf_counter()
    r = client.post("/v1/chat/completions", json=_payload(model, prompt, tools, thinking, False))
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    choice = r.json()["choices"][0]
    calls = choice["message"].get("tool_calls") or []
    chose = calls[0]["function"]["name"] if calls else None
    return ms, chose == expected, choice.get("finish_reason")


def measure_ttft(client, model, prompt, tools, thinking):
    """Time to first content token, streaming. None if the stream yields none."""
    t0 = time.perf_counter()
    with client.stream("POST", "/v1/chat/completions",
                       json=_payload(model, prompt, tools, thinking, True)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0].get("delta", {})
            if delta.get("content") or delta.get("tool_calls"):
                return (time.perf_counter() - t0) * 1000
    return None


def pct(vals, p):
    if not vals:
        return float("nan")
    return statistics.quantiles(vals, n=100)[p - 1] if len(vals) > 2 else max(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080", help="vLLM base URL")
    ap.add_argument("--model", default="qwen3-14b")
    ap.add_argument("--repeats", type=int, default=10, help="per cell; 10 is enough to see the shape, 20+ for p95")
    ap.add_argument("--json", help="also write raw results here")
    ap.add_argument("--skip-thinking", action="store_true", help="skip the thinking-on arm (it is slow, by design)")
    args = ap.parse_args()

    client = httpx.Client(base_url=args.url, timeout=300.0)
    try:
        client.get("/health").raise_for_status()
    except Exception as e:
        sys.exit(f"vLLM not reachable at {args.url}: {e}\nStart it first: docker compose --profile local-llm up -d llm")

    # Tool-count arms test section 8.3's ceiling directly. 6 = the design's
    # "core" band; 10 = the stated ceiling; 20 = what the system actually
    # presents today (see WORK_PLAN objection 4).
    arms = [("6 tools", CORE_TOOLS),
            ("10 tools", CORE_TOOLS + FILLER_TOOLS[:4]),
            ("20 tools", CORE_TOOLS + FILLER_TOOLS)]
    thinking_arms = [False] if args.skip_thinking else [False, True]

    raw, summary = [], []
    for thinking in thinking_arms:
        for arm_name, tools in arms:
            lat, hits, truncated = [], 0, 0
            n = 0
            for prompt, expected in PROBES:
                for _ in range(args.repeats):
                    ms, ok, finish = measure_round(client, args.model, prompt, expected, tools, thinking)
                    lat.append(ms); hits += ok; n += 1
                    truncated += (finish == "length")
                    raw.append({"thinking": thinking, "arm": arm_name, "prompt": prompt,
                                "expected": expected, "ms": ms, "correct": ok, "finish": finish})
            summary.append({
                "thinking": thinking, "arm": arm_name, "n": n,
                "p50": statistics.median(lat), "p95": pct(lat, 95),
                "accuracy": hits / n, "truncated": truncated,
            })
            print(f"  measured thinking={thinking} {arm_name}: "
                  f"p50={statistics.median(lat):.0f}ms acc={hits/n:.0%}", file=sys.stderr)

    ttft = [t for _ in range(args.repeats)
            for t in [measure_ttft(client, args.model, "Tell me about the weather in London.", CORE_TOOLS, False)]
            if t is not None]

    print("\n=== Tool-decision round (non-streaming, one round) ===")
    print(f"{'thinking':>9} {'tools':>9} {'p50 ms':>9} {'p95 ms':>9} {'accuracy':>9} {'truncated':>10}")
    for s in summary:
        print(f"{str(s['thinking']):>9} {s['arm']:>9} {s['p50']:>9.0f} {s['p95']:>9.0f} "
              f"{s['accuracy']:>8.0%} {s['truncated']:>10}")

    print("\n=== Streaming time-to-first-token (tools in context) ===")
    if ttft:
        print(f"  p50 {statistics.median(ttft):.0f} ms   p95 {pct(ttft, 95):.0f} ms   n={len(ttft)}")
    else:
        print("  NO CONTENT DELTAS RECEIVED — the hermes parser may be returning raw text under")
        print("  streaming (design section 4.7's documented risk). This is the P6-3 answer: take")
        print("  the hold-back-buffer branch, not the stream-every-round branch.")

    # ---- the verdicts this script exists to deliver -------------------------
    base = next(s for s in summary if not s["thinking"] and s["arm"] == "10 tools")
    print("\n=== VERDICT ===")
    budget = 1500
    if base["p50"] <= budget:
        print(f"PHASE 7: GO. Tool round p50 {base['p50']:.0f}ms is within the {budget}ms design budget.")
    else:
        print(f"PHASE 7: NO-GO as designed. Tool round p50 {base['p50']:.0f}ms exceeds the {budget}ms")
        print("  budget in section 8.1. The 3s voice target is not reachable with this model+hardware.")
        print("  Rescope before building Phases 6-7: smaller model, fewer tools, or drop voice.")

    acc = {s["arm"]: s["accuracy"] for s in summary if not s["thinking"]}
    print(f"\nTOOL CEILING (section 8.3): 6 tools {acc.get('6 tools', 0):.0%} | "
          f"10 tools {acc.get('10 tools', 0):.0%} | 20 tools {acc.get('20 tools', 0):.0%} accuracy")
    if acc.get("20 tools", 1) < acc.get("6 tools", 0) - 0.1:
        print("  -> Degradation confirmed. P2-0 (progressive disclosure) must land before Phase 2 adds tools.")
    else:
        print("  -> No clear degradation at 20 tools. P2-0 may be lower priority than WORK_PLAN claims;")
        print("     revisit objection 4 with this data rather than the design's assumption.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"summary": summary, "ttft_ms": ttft, "raw": raw}, f, indent=2)
        print(f"\nraw results -> {args.json}")
    print("\nPaste the tables above into SYSTEM_DESIGN.md section 8.1 and 8.3, replacing the estimates.")


if __name__ == "__main__":
    main()
