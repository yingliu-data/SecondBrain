#!/usr/bin/env python3
"""harness/image/build_snapshot.py -- build the BASE snapshot (docs/IMAGE.md).

    build_snapshot.py build-base [--cache DIR] [--idle-timeout S] [--settle S]
                                 [--fixed-delay S] [--keep-build-dir]
    build_snapshot.py check-base [--cache DIR]

Boots the rootfs in Firecracker (cwd = a scratch session dir under the cache,
drives `rootfs.ext4` / `workspace.img` and vsock `../ipc/v.sock` relative to
it), waits until the guest sits in `--idle-wait vsock` (vsock connect attempts
with no listener show up as packets in Firecracker's metrics), pauses,
snapshots into `{cache}/base/{vmstate,mem}` and writes `MANIFEST.json`.

Configuration is the agent-api's HARNESS_* environment (`make snapshot` fills
in out/ defaults). The logic lives in agent-api/app/host/sandbox/base_snapshot.py
(standard library, `protocol` and the firecracker backend only); this script
puts agent-api/ and harness/ on sys.path and runs its main(), so neither the
agent-api's dependencies nor a virtualenv are needed. Inside the agent-api
container the same thing is `python -m app.host.sandbox.base_snapshot build-base`.
HARNESS_AGENT_API_ROOT points at another agent-api checkout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS_ROOT = HERE.parent                                  # protocol/
DEFAULT_AGENT_API = HARNESS_ROOT.parent / "agent-api"       # app/host/sandbox/


def main() -> int:
    agent_api = Path(os.environ.get("HARNESS_AGENT_API_ROOT", DEFAULT_AGENT_API))
    module = agent_api / "app" / "host" / "sandbox" / "base_snapshot.py"
    if not module.is_file():
        print(f"[snapshot] error: {module} not found (set HARNESS_AGENT_API_ROOT)", file=sys.stderr)
        return 2
    if not os.access("/dev/kvm", os.R_OK | os.W_OK) and "--help" not in sys.argv:
        print("[snapshot] warning: /dev/kvm is not accessible; Firecracker will fail to boot",
              file=sys.stderr)
    sys.path[:0] = [str(agent_api), str(HARNESS_ROOT)]
    from app.host.sandbox.base_snapshot import main as snapshot_main
    return snapshot_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
