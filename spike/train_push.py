# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx[http2]>=0.28", "pyjwt[crypto]>=2.8"]
# ///
"""PW-4 — THROWAWAY SPIKE. Delete this directory when the experiment ends.

THE POINT: WORK_PLAN.md objection 1 says roughly sixteen days of infrastructure
(contracts package, MCP gateway, tool servers) currently precede the first thing
you would actually notice as a user -- the morning train push. This script is
that push, with none of the infrastructure: a cron entry, one REST call, a
template string, one APNs request. No MCP, no gateway, no model, no microVM.

Run it for ONE WEEK, then answer honestly: did the 07:45 notification change
anything you did? If yes, Phases 1-3 are justified and you already have a
working Darwin call to lift into P2-1. If no, stop -- the remaining ~50 days are
buying a worse Google Maps with a quota, and the right plan is P0 + P6-1 only.

    cp spike/config.example.json spike/config.json   # then fill it in
    uv run spike/train_push.py --dry-run             # prints, sends nothing
    uv run spike/train_push.py

Credentials you need (neither is in this repo):
  * Rail Data Marketplace consumer key -- subscribe to the Live Departure Board
    REST product. NOT legacy SOAP OpenLDBWS; those tokens no longer work.
  * APNs auth key .p8 + key id + team id, from the Apple Developer portal, plus
    the device token your IndexApp build registers.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

CONFIG = Path(__file__).parent / "config.json"
DARWIN_BASE = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/LDBWS/api/20220120"


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"No {CONFIG}. Copy spike/config.example.json and fill it in.")
    return json.loads(CONFIG.read_text())


# ------------------------------------------------------------------ departures
def fetch_departures(cfg: dict) -> list[dict]:
    """Live departures from one CRS code to another. Returns [] on any failure --
    a spike that crashes at 07:45 teaches you nothing about whether the push is
    useful."""
    try:
        r = httpx.get(
            f"{DARWIN_BASE}/GetDepBoardWithDetails/{cfg['origin_crs']}",
            params={"numRows": 6, "filterCrs": cfg["destination_crs"], "filterType": "to"},
            headers={"x-apikey": cfg["darwin_api_key"]},
            timeout=15.0,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"darwin fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return []

    services = r.json().get("trainServices") or []
    out = []
    for s in services:
        out.append({
            "scheduled": s.get("std"),
            "expected": s.get("etd"),
            "platform": s.get("platform"),
            "cancelled": bool(s.get("isCancelled")),
            "reason": s.get("cancelReason") or s.get("delayReason"),
        })
    return out


# -------------------------------------------------------------------- template
def render(deps: list[dict], cfg: dict) -> tuple[str, str]:
    """Pure code, no model. SYSTEM_DESIGN principle 1: deterministic work never
    touches the model. The LLM's only job in the real Phase 3 version is one
    sentence of phrasing when something is unusual -- and it falls back to
    exactly this template when inference is down."""
    route = f"{cfg['origin_name']} to {cfg['destination_name']}"
    if not deps:
        return route, "No departure data right now."

    first = deps[0]
    if first["cancelled"]:
        nxt = next((d for d in deps[1:] if not d["cancelled"]), None)
        body = f"{first['scheduled']} CANCELLED"
        if first["reason"]:
            body += f" ({first['reason']})"
        if nxt:
            body += f". Next: {nxt['expected'] or nxt['scheduled']}"
        return route, body

    when = first["expected"] or "On time"
    late = when not in ("On time", "", None) and when != first["scheduled"]
    body = f"{first['scheduled']} — {when}" if late else f"{first['scheduled']} on time"
    if first["platform"]:
        body += f", plat {first['platform']}"
    if len(deps) > 1:
        body += f". Then {deps[1]['expected'] or deps[1]['scheduled']}"
    return route, body


# ------------------------------------------------------------------------ APNs
def push(title: str, body: str, cfg: dict) -> bool:
    key = Path(cfg["apns_key_file"]).read_text()
    token = jwt.encode(
        {"iss": cfg["apns_team_id"], "iat": int(time.time())},
        key, algorithm="ES256", headers={"kid": cfg["apns_key_id"]},
    )
    host = "api.sandbox.push.apple.com" if cfg.get("apns_sandbox") else "api.push.apple.com"
    try:
        # APNs requires HTTP/2.
        with httpx.Client(http2=True, timeout=15.0) as c:
            r = c.post(
                f"https://{host}/3/device/{cfg['device_token']}",
                headers={"authorization": f"bearer {token}",
                         "apns-topic": cfg["bundle_id"],
                         "apns-push-type": "alert",
                         "apns-priority": "10"},
                json={"aps": {"alert": {"title": title, "body": body}, "sound": "default"}},
            )
    except Exception as e:
        print(f"apns request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    if r.status_code != 200:
        print(f"apns rejected: {r.status_code} {r.text}", file=sys.stderr)
        return False
    return True


# ------------------------------------------------------------------- the diary
def log_run(title: str, body: str, sent: bool) -> None:
    """The actual output of this experiment is not the notification. It is one
    week of these lines plus your own honest answer to 'did I act on it?'"""
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                       "title": title, "body": body, "sent": sent})
    with (Path(__file__).parent / "diary.jsonl").open("a") as f:
        f.write(line + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, send nothing")
    args = ap.parse_args()

    cfg = load_config()
    deps = fetch_departures(cfg)
    title, body = render(deps, cfg)

    if args.dry_run:
        print(f"{title}\n{body}")
        print(f"\n[dry run — {len(deps)} services fetched, nothing sent]")
        return

    sent = push(title, body, cfg)
    log_run(title, body, sent)
    print(f"{'sent' if sent else 'FAILED'}: {title} / {body}")


if __name__ == "__main__":
    main()
