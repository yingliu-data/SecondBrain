"""sandbox/base_snapshot — build and check the shared BASE snapshot.

    python -m app.host.sandbox.base_snapshot build-base [--cache DIR] [--idle-timeout S]
                                                      [--settle S] [--fixed-delay S]
                                                      [--keep-build-dir]
    python -m app.host.sandbox.base_snapshot check-base [--cache DIR]

(`python -m app.host.sandbox.firecracker build-base|check-base` and
harness/image/build_snapshot.py run this same main.) Configuration from the
HARNESS_* environment (FirecrackerConfig.from_env).

  wait_guest_idle(sb, ...)       block until the guest sits in `--idle-wait vsock`
  build_base_snapshot(config)    boot the rootfs in a scratch session dir, wait for
                                 the idle-wait, snapshot into {cache}/base + MANIFEST.json
  main(argv)                     the CLI above

Standard library, protocol and the firecracker backend only (no app.config):
harness/image/build_snapshot.py runs it with agent-api/ and harness/ on sys.path.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import time
from pathlib import Path

from app.host.sandbox.firecracker import (
    BASE_GUEST_CID, CONSOLE_NAME, MANIFEST_NAME, SNAPSHOT_FORMAT, FirecrackerConfig,
    FirecrackerSandbox, SandboxError, image_version, image_version_from_shas, sha256_file,
)

__all__ = ["build_base_snapshot", "main", "wait_guest_idle"]


def _vsock_activity(metrics: dict | None) -> int:
    """Guest vsock traffic in one flush window (Firecracker metrics are deltas)."""
    if not metrics:
        return 0
    v = metrics.get("vsock") or {}
    return sum(int(v.get(k) or 0) for k in
               ("tx_queue_event_count", "tx_packets_count", "rx_packets_count"))


def wait_guest_idle(sb: FirecrackerSandbox, *, timeout_s: float = 120.0, poll_s: float = 0.2,
                    windows: int = 3, settle_s: float = 1.0, fixed_delay_s: float | None = None,
                    log=None) -> str:
    """Block until the guest sits in its `--idle-wait vsock` loop.

    With no host listener on the control port's socket (v.sock_5000,
    protocol.ports), each connect attempt (every 5 ms, the guest's
    IDLE_POLL_S) is a REQUEST the VMM answers with RST: vsock packets in
    every metrics window, and nothing else in the guest touches vsock before hello. `windows`
    consecutive windows with traffic -> idle; then `settle_s` for the rest of
    the system to quiesce. `fixed_delay_s` skips detection (sleep only).
    Returns how idleness was decided ("metrics" | "fixed")."""
    say = log or (lambda *_: None)
    if fixed_delay_s is not None:
        deadline = time.monotonic() + fixed_delay_s
        while time.monotonic() < deadline:
            if not sb.is_alive():
                raise SandboxError(f"VM exited during settle (see {sb.vm_dir / CONSOLE_NAME})")
            time.sleep(0.05)
        return "fixed"
    deadline = time.monotonic() + timeout_s
    streak = 0
    sb.flush_metrics()               # discard the boot window
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        if not sb.is_alive():
            raise SandboxError(f"VM exited before idle (see {sb.vm_dir / CONSOLE_NAME})")
        act = _vsock_activity(sb.flush_metrics())
        streak = streak + 1 if act > 0 else 0
        say(f"vsock activity {act} (streak {streak}/{windows})")
        if streak >= windows:
            time.sleep(settle_s)
            return "metrics"
    raise SandboxError(
        f"guest never reached idle-wait within {timeout_s}s (no vsock connect attempts; "
        f"see {sb.vm_dir / CONSOLE_NAME}); retry with a fixed delay")


def build_base_snapshot(config: FirecrackerConfig, cache_dir: str | Path | None = None, *,
                        idle_timeout_s: float = 120.0, settle_s: float = 1.0,
                        fixed_delay_s: float | None = None, keep_build_dir: bool = False,
                        log=None) -> dict:
    """Boot the rootfs in a scratch session dir under the cache, wait for the
    guest's idle-wait, snapshot into `{cache}/base` (atomic swap) with
    MANIFEST.json. Returns the manifest."""
    say = log or (lambda *_: None)
    cache = Path(cache_dir or config.vm_cache)
    cache.mkdir(parents=True, exist_ok=True)
    cfg = copy.deepcopy(config)
    cfg.vm_cache = str(cache)
    cfg.guest_cid = BASE_GUEST_CID
    cfg.validate()
    kernel_sha = sha256_file(cfg.kernel)
    rootfs_sha = sha256_file(cfg.rootfs)
    version = cfg.image_version or image_version(cfg)
    computed = image_version_from_shas(kernel_sha, rootfs_sha)
    if version != computed:
        say(f"note: image_version {version} (configured / IMAGE_VERSION file) != "
            f"computed {computed}; the configured one is recorded")
    build_dir = cache / ".build-base"
    shutil.rmtree(build_dir, ignore_errors=True)
    sb = FirecrackerSandbox(cfg)
    try:
        say(f"booting rootfs in {build_dir}")
        sb.start(build_dir)
        say(f"booted in {sb.last_vm_event['ms']} ms; waiting for the guest's idle-wait")
        how = wait_guest_idle(sb, timeout_s=idle_timeout_s, settle_s=settle_s,
                              fixed_delay_s=fixed_delay_s, log=say)
        manifest = {
            "format": SNAPSHOT_FORMAT,
            "image_version": version,
            "kernel_sha256": kernel_sha,
            "rootfs_sha256": rootfs_sha,
            "firecracker_version": sb._fc_version(),
            "guest_cid": BASE_GUEST_CID,
            "vcpus": cfg.vcpus,
            "mem_mib": cfg.mem_mib,
            "workspace_bytes": sb.workspace_image.stat().st_size,
            "boot_args": cfg.boot_args,
            "idle_detect": how,
        }
        sb._snapshot_into(cfg.base_snapshot_dir, MANIFEST_NAME, manifest, source="base")
        manifest = json.loads((cfg.base_snapshot_dir / MANIFEST_NAME).read_text())
        say(f"base snapshot written to {cfg.base_snapshot_dir} in {sb.last_vm_event['ms']} ms")
        return manifest
    finally:
        sb._kill()
        if not keep_build_dir:
            shutil.rmtree(build_dir, ignore_errors=True)


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    """`python -m app.host.sandbox.base_snapshot build-base|check-base` (also
    `python -m app.host.sandbox.firecracker ...` and
    harness/image/build_snapshot.py). Configuration from HARNESS_*."""
    import argparse
    ap = argparse.ArgumentParser(prog="firecracker-sandbox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-base", help="build {HARNESS_VM_CACHE}/base")
    b.add_argument("--cache", default=None, help="cache dir (default HARNESS_VM_CACHE)")
    b.add_argument("--idle-timeout", type=float, default=120.0)
    b.add_argument("--settle", type=float, default=1.0, help="seconds after idle is detected")
    b.add_argument("--fixed-delay", type=float, default=None,
                   help="skip vsock-metrics detection; snapshot after this many seconds")
    b.add_argument("--keep-build-dir", action="store_true")
    c = sub.add_parser("check-base", help="is the base snapshot usable with this config?")
    c.add_argument("--cache", default=None)
    args = ap.parse_args(argv)
    cfg = FirecrackerConfig.from_env()
    if args.cache:
        cfg.vm_cache = args.cache
    log = lambda m: print(f"[snapshot] {m}", file=sys.stderr, flush=True)  # noqa: E731
    try:
        if args.cmd == "build-base":
            manifest = build_base_snapshot(
                cfg, idle_timeout_s=args.idle_timeout, settle_s=args.settle,
                fixed_delay_s=args.fixed_delay, keep_build_dir=args.keep_build_dir, log=log)
            print(json.dumps(manifest, indent=2, sort_keys=True))
            return 0
        sb = FirecrackerSandbox(cfg)
        meta = sb._check_snapshot(cfg.base_snapshot_dir, "base")
        print(json.dumps(meta, indent=2, sort_keys=True))
        return 0
    except SandboxError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
