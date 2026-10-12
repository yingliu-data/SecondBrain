#!/usr/bin/env python3
"""harness/image/measure_imports.py -- what a restore pays for imports
(docs/IMAGE.md "guest.pyz").

    measure_imports.py PYZ [--runs N] [--python CMD] [--json]

Runs a fresh interpreter per run (`python3 -I`, or CMD, e.g. the guest image:
--python "docker run --rm -i -v $PWD/out:/o:ro secondbrain/harness-rootfs:local python3 -I"
with PYZ given as the path inside it) and times two phases against PYZ:

  pre_snapshot_ms  `import guest.main` and, when it has one, guest.main.preload():
                   what the guest does before its idle-wait, i.e. what the
                   base snapshot already holds in memory
  post_hello_ms    importing the session modules (SESSION_MODULES: what
                   run_session and the coordinator need) after that: the cost
                   every restore pays between hello and the first event

Prints min / median per phase (ms) and how many modules phase 2 loaded.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys

SESSION_MODULES = (
    "protocol.fold", "protocol.bulk", "guest.coordinator", "guest.bulk", "guest.mcp",
    "guest.ops", "guest.provider", "guest.hooks", "guest.context", "guest.tools",
)

PROBE = r"""
import sys, time, json
sys.path.insert(0, sys.argv[1])
mods = sys.argv[2].split(",")
t0 = time.perf_counter()
import guest.main
pre = getattr(guest.main, "preload", None)
if pre is not None:
    pre()
t1 = time.perf_counter()
before = set(sys.modules)
import importlib
for m in mods:
    importlib.import_module(m)
t2 = time.perf_counter()
print(json.dumps({"pre_snapshot_ms": (t1 - t0) * 1e3, "post_hello_ms": (t2 - t1) * 1e3,
                  "post_hello_modules": sorted(set(sys.modules) - before),
                  "preload": pre is not None, "version": sys.version.split()[0]}))
"""


def run_once(pyz: str, python: list[str]) -> dict:
    out = subprocess.run(python + ["-c", PROBE, pyz, ",".join(SESSION_MODULES)],
                         check=True, capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout.strip().splitlines()[-1])


def measure(pyz: str, runs: int = 5, python: list[str] | None = None) -> dict:
    python = python or [sys.executable, "-I"]
    samples = [run_once(pyz, python) for _ in range(runs)]
    res = {"pyz": pyz, "runs": runs, "version": samples[0]["version"],
           "preload": samples[0]["preload"],
           "post_hello_modules": samples[0]["post_hello_modules"]}
    for k in ("pre_snapshot_ms", "post_hello_ms"):
        xs = [s[k] for s in samples]
        res[k] = {"min": round(min(xs), 2), "median": round(statistics.median(xs), 2)}
    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="measure_imports.py")
    p.add_argument("pyz")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--python", default=None, help="interpreter command (default: this python -I)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    res = measure(a.pyz, a.runs, shlex.split(a.python) if a.python else None)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{res['pyz']} (python {res['version']}, preload={res['preload']}, {a.runs} runs)")
        for k in ("pre_snapshot_ms", "post_hello_ms"):
            print(f"  {k:16s} min {res[k]['min']:8.2f}  median {res[k]['median']:8.2f}")
        print(f"  post-hello modules loaded: {len(res['post_hello_modules'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
