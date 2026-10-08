#!/usr/bin/env python3
"""harness/image/build_snapshot.py -- build the BASE snapshot (docs/IMAGE.md, M3-CONTRACTS §1).

    build_snapshot.py build-base [--cache DIR] [--idle-timeout S] [--settle S]
                                 [--fixed-delay S] [--keep-build-dir]
    build_snapshot.py check-base [--cache DIR]

Boots the rootfs in Firecracker (cwd = a scratch session dir under the cache,
drives `rootfs.ext4` / `workspace.img` and vsock `../ipc/v.sock` relative to
it), waits until the guest sits in `--idle-wait vsock` (vsock connect attempts
with no listener show up as packets in Firecracker's metrics), pauses,
snapshots into `{cache}/base/{vmstate,mem}` and writes `MANIFEST.json`.

Configuration is the agent-api's HARNESS_* environment (`make snapshot` fills
in out/ defaults). The logic lives in agent-api/app/host/sandbox/firecracker.py
(stdlib-only); this script loads that file directly, so neither the agent-api
package nor its dependencies need to be installed. Inside the agent-api
container the same thing is `python -m app.host.sandbox.firecracker build-base`.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MODULE = HERE.parent.parent / "agent-api" / "app" / "host" / "sandbox" / "firecracker.py"


def load_sandbox_module(path: Path):
    spec = importlib.util.spec_from_file_location("harness_fc_sandbox", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    path = Path(os.environ.get("HARNESS_SANDBOX_MODULE", DEFAULT_MODULE))
    if not path.is_file():
        print(f"[snapshot] error: {path} not found (set HARNESS_SANDBOX_MODULE)", file=sys.stderr)
        return 2
    if not os.access("/dev/kvm", os.R_OK | os.W_OK) and "--help" not in sys.argv:
        print("[snapshot] warning: /dev/kvm is not accessible; Firecracker will fail to boot",
              file=sys.stderr)
    return load_sandbox_module(path).main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
