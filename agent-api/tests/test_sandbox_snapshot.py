"""FirecrackerSandbox: snapshot, restore, base-snapshot build.

Same fake binary as test_sandbox_firecracker.py: `/snapshot/create` writes the
files at the paths it is given (relative paths resolve against its cwd, like
the real binary), `/snapshot/load` checks they exist and records its cwd and
what `workspace.img` / `rootfs.ext4` resolve to there. No KVM.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.host.sandbox.firecracker import (
    API_ORDER,
    BASE_GUEST_CID,
    RESTORE_ORDER,
    SNAPSHOT_ORDER,
    FirecrackerConfig,
    FirecrackerSandbox,
    SandboxError,
    derive_guest_cid,
    image_version,
    image_version_from_shas,
    sha256_file,
)
from app.host.sandbox.base_snapshot import build_base_snapshot, main as fc_main, wait_guest_idle
from test_sandbox_firecracker import (  # noqa: F401  (fixtures)
    _config,
    _from_vm_dir,
    _requests,
    _wait_exit,
    fake_fc,
    fake_mkfs,
    session_dir,
)


def _m3_config(fake_fc, fake_mkfs, tmp_path, **over) -> FirecrackerConfig:
    over.setdefault("image_version", "img-1")
    over.setdefault("vm_cache", str(tmp_path / "vmcache"))
    return _config(fake_fc, fake_mkfs, tmp_path, **over)


def _records(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(l) for l in record.read_text().splitlines()]


def _launches(record: Path) -> int:
    return sum(1 for r in _records(record) if "argv" in r)


def _after_last_launch(record: Path) -> list[dict]:
    """Requests sent to the most recently launched process."""
    out: list[dict] = []
    for r in _records(record):
        if "argv" in r:
            out = []
        elif "method" in r:
            out.append(r)
    return out


def _load_info(record: Path) -> list[dict]:
    return [r for r in _records(record) if "cwd" in r]


def _make_base(fake_fc, fake_mkfs, tmp_path, **over) -> FirecrackerConfig:
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path, **over)
    build_base_snapshot(cfg, fixed_delay_s=0.05)
    return cfg


# --------------------------------------------------------------- snapshot()


def test_snapshot_pauses_creates_with_relative_paths_and_kills(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    snap = session_dir / "vm" / "snapshot"
    sb.snapshot(snap)
    assert not sb.is_alive()
    assert sb.returncode == -9                       # the paused VM is killed, not shut down

    reqs = _after_last_launch(fake_fc[1])
    assert [(r["method"], r["path"]) for r in reqs] == list(API_ORDER) + list(SNAPSHOT_ORDER)
    assert reqs[len(API_ORDER)]["body"] == {"state": "Paused"}
    create = reqs[-1]["body"]
    assert create["snapshot_type"] == "Full"
    for key in ("snapshot_path", "mem_file_path"):
        assert not os.path.isabs(create[key])        # relative to the FC cwd = {session}/vm
        assert _from_vm_dir(session_dir, create[key]).startswith(
            str(session_dir / "vm" / ".snapshot.tmp"))
    # the fake wrote them from cwd = vm/; they were swapped into place
    assert (snap / "vmstate").read_bytes().endswith(str(session_dir / "vm").encode())
    assert (snap / "mem").exists()
    assert not (session_dir / "vm" / ".snapshot.tmp").exists()
    meta = json.loads((snap / "META.json").read_text())
    assert meta["image_version"] == "img-1"
    assert meta["firecracker_version"] == "v1.17.0"
    assert meta["guest_cid"] == derive_guest_cid("default_abc123")
    assert isinstance(meta["created_ms"], int)
    assert sb.last_vm_event["op"] == "snapshot" and sb.last_vm_event["ms"] >= 0
    assert not (session_dir / "vm" / "firecracker.sock").exists()
    assert sb.has_snapshot(snap)


def test_snapshot_requires_a_running_vm(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    with pytest.raises(SandboxError, match="not running"):
        FirecrackerSandbox(cfg).snapshot(session_dir / "vm" / "snapshot")


def test_snapshot_create_failure_resumes_and_keeps_old(fake_fc, fake_mkfs, session_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FC_FAIL_PATH", "/snapshot/create")
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    snap = session_dir / "vm" / "snapshot"
    snap.mkdir(parents=True)
    (snap / "keep").write_text("old")
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    try:
        with pytest.raises(SandboxError, match="snapshot/create"):
            sb.snapshot(snap)
        assert sb.is_alive()
        reqs = _after_last_launch(fake_fc[1])
        assert reqs[-1] == {"method": "PATCH", "path": "/vm", "body": {"state": "Resumed"}}
        assert (snap / "keep").read_text() == "old"
        assert not (session_dir / "vm" / ".snapshot.tmp").exists()
    finally:
        sb.stop()


# --------------------------------------------------------------- restore()


def test_restore_from_session_snapshot(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    snap = session_dir / "vm" / "snapshot"
    sb.snapshot(snap)
    cid = sb.guest_cid

    sb2 = FirecrackerSandbox(cfg)
    sb2.restore(session_dir, snap)
    try:
        assert sb2.is_alive()
        reqs = _after_last_launch(fake_fc[1])
        assert [(r["method"], r["path"]) for r in reqs] == list(RESTORE_ORDER)
        assert reqs[0]["body"] == {
            "snapshot_path": str(snap / "vmstate"),
            "mem_backend": {"backend_type": "File", "backend_path": str(snap / "mem")},
            "resume_vm": True,
            "vsock_override": {"uds_path": "../ipc/v.sock"},
        }
        info = _load_info(fake_fc[1])[-1]
        assert info["cwd"] == str(session_dir / "vm")
        assert info["workspace_exists"] is True
        assert sb2.guest_cid == cid
        ms = sb2.last_vm_event["ms"]
        assert isinstance(ms, float) and ms >= 0
        assert sb2.last_vm_event == {"op": "restore", "source": "session", "ms": ms}
        # single-use: the running VM diverges from that vmstate
        assert not sb2.has_snapshot(snap)
        assert (snap / "META.json.consumed").exists()
        # snapshot again into the same dir: staging + swap, never truncating in place
        sb2.snapshot(snap)
        assert sb2.has_snapshot(snap)
    finally:
        sb2.stop()


def test_restore_without_snapshot_is_sandbox_error(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    assert not sb.has_snapshot()
    assert not sb.has_snapshot(session_dir / "vm" / "snapshot")
    with pytest.raises(SandboxError, match="no base snapshot"):
        sb.restore(session_dir)
    with pytest.raises(SandboxError, match="no session snapshot"):
        sb.restore(session_dir, session_dir / "vm" / "snapshot")
    assert _launches(fake_fc[1]) == 0                # nothing was launched
    assert not sb.is_alive()


def test_manifest_image_version_mismatch(fake_fc, fake_mkfs, session_dir, tmp_path):
    _make_base(fake_fc, fake_mkfs, tmp_path)
    launched = _launches(fake_fc[1])
    sb = FirecrackerSandbox(_m3_config(fake_fc, fake_mkfs, tmp_path, image_version="img-2"))
    assert not sb.has_snapshot()
    with pytest.raises(SandboxError, match="image_version"):
        sb.restore(session_dir)
    assert _launches(fake_fc[1]) == launched         # refused before launching


def test_manifest_firecracker_version_mismatch(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _make_base(fake_fc, fake_mkfs, tmp_path)
    cfg.firecracker_version = "v1.18.0"              # e.g. the binary was upgraded
    with pytest.raises(SandboxError, match="firecracker_version"):
        FirecrackerSandbox(cfg).restore(session_dir)


def test_base_restore_refuses_a_different_workspace_size(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _make_base(fake_fc, fake_mkfs, tmp_path)
    (session_dir / "vm").mkdir(parents=True)
    (session_dir / "vm" / "workspace.img").write_bytes(b"A DIFFERENT SIZE")
    with pytest.raises(SandboxError, match="workspace.img is 16 bytes"):
        FirecrackerSandbox(cfg).restore(session_dir)


def test_load_failure_is_sandbox_error_and_kills(fake_fc, fake_mkfs, session_dir, tmp_path, monkeypatch):
    cfg = _make_base(fake_fc, fake_mkfs, tmp_path)
    monkeypatch.setenv("FAKE_FC_FAIL_PATH", "/snapshot/load")
    sb = FirecrackerSandbox(cfg)
    with pytest.raises(SandboxError) as info:
        sb.restore(session_dir)
    assert info.value.status == 400
    _wait_exit(sb)
    assert not sb.is_alive()


def test_restore_does_not_need_a_kernel(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _make_base(fake_fc, fake_mkfs, tmp_path)
    cfg.kernel = ""
    sb = FirecrackerSandbox(cfg)
    sb.restore(session_dir)
    sb.stop()


# ------------------------------------------------------------ base snapshot


def test_base_snapshot_build_and_manifest(fake_fc, fake_mkfs, tmp_path):
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    manifest = build_base_snapshot(cfg, fixed_delay_s=0.05)
    base = tmp_path / "vmcache" / "base"
    assert (base / "vmstate").exists() and (base / "mem").exists()
    assert json.loads((base / "MANIFEST.json").read_text()) == manifest
    assert manifest["image_version"] == "img-1"
    assert manifest["firecracker_version"] == "v1.17.0"
    assert manifest["guest_cid"] == BASE_GUEST_CID
    assert manifest["kernel_sha256"] == sha256_file(tmp_path / "vmlinux")
    assert manifest["rootfs_sha256"] == sha256_file(tmp_path / "rootfs.ext4")
    assert manifest["workspace_bytes"] == len(b"FAKE-EXT4")
    assert manifest["idle_detect"] == "fixed"
    assert isinstance(manifest["created_ms"], int)
    assert not (tmp_path / "vmcache" / ".build-base").exists()
    # the build VM booted with relative drive / vsock paths (they end up in the vmstate)
    boot = {r["path"]: r["body"] for r in _records(fake_fc[1]) if r.get("method") == "PUT"}
    assert boot["/drives/workspace"]["path_on_host"] == "workspace.img"
    assert boot["/drives/rootfs"]["path_on_host"] == "rootfs.ext4"
    assert boot["/vsock"] == {"guest_cid": BASE_GUEST_CID, "uds_path": "../ipc/v.sock"}
    assert FirecrackerSandbox(cfg).has_snapshot()
    # rebuilding swaps atomically
    build_base_snapshot(cfg, fixed_delay_s=0.05)
    assert FirecrackerSandbox(cfg).has_snapshot()
    assert not (tmp_path / "vmcache" / ".base.tmp").exists()
    assert not (tmp_path / "vmcache" / ".base.old").exists()


def test_two_sessions_restore_from_one_base(fake_fc, fake_mkfs, tmp_path):
    cfg = _make_base(fake_fc, fake_mkfs, tmp_path)
    base = tmp_path / "vmcache" / "base"
    sessions = [tmp_path / "sessions" / "u" / f"default_s{i}" for i in (1, 2)]
    boxes = []
    try:
        for sd in sessions:
            sb = FirecrackerSandbox(cfg)
            sb.restore(sd)
            boxes.append(sb)
            assert sb.guest_cid == BASE_GUEST_CID
            assert sb.last_vm_event["source"] == "base"
            assert (sd / "vm" / "workspace.img").read_bytes() == b"FAKE-EXT4"   # per session
        loads = [r["body"] for r in _records(fake_fc[1]) if r.get("path") == "/snapshot/load"]
        assert [b["snapshot_path"] for b in loads] == [str(base / "vmstate")] * 2
        assert [b["vsock_override"] for b in loads] == [{"uds_path": "../ipc/v.sock"}] * 2
        infos = _load_info(fake_fc[1])
        assert [i["cwd"] for i in infos] == [str(sd / "vm") for sd in sessions]
        assert all(i["rootfs_link"] == str(tmp_path / "rootfs.ext4") for i in infos)
        assert all(b.is_alive() for b in boxes)
    finally:
        for b in boxes:
            b.stop()


def test_wait_guest_idle_from_vsock_metrics(fake_fc, fake_mkfs, session_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FC_VSOCK_PKTS", "3")      # connect attempts answered with RST
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    try:
        assert wait_guest_idle(sb, timeout_s=5, poll_s=0.02, settle_s=0) == "metrics"
        flushes = [r for r in _requests(fake_fc[1])
                   if r["path"] == "/actions" and r["body"] == {"action_type": "FlushMetrics"}]
        assert len(flushes) >= 4                       # one discarded + three windows
    finally:
        sb.stop()


def test_wait_guest_idle_times_out_without_vsock_traffic(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _m3_config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    try:
        with pytest.raises(SandboxError, match="idle-wait"):
            wait_guest_idle(sb, timeout_s=0.3, poll_s=0.05, settle_s=0)
    finally:
        sb.stop()


# ------------------------------------------------------------- identities


def test_image_version_sources(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    kernel = out / "vmlinux"
    kernel.write_bytes(b"K")
    rootfs = out / "rootfs.ext4"
    rootfs.write_bytes(b"R")
    cfg = FirecrackerConfig(kernel=str(kernel), rootfs=str(rootfs))
    computed = image_version(cfg)
    assert computed == image_version_from_shas(sha256_file(kernel), sha256_file(rootfs))
    assert len(computed) == 16
    # the same formula as harness/image/build.sh `version`
    script = ('k=$(sha256sum "$1" | cut -d" " -f1); r=$(sha256sum "$2" | cut -d" " -f1); '
              'printf "%s\\n%s\\n" "$k" "$r" | sha256sum | cut -c1-16')
    sh = subprocess.run(["bash", "-c", script, "x", str(kernel), str(rootfs)],
                        capture_output=True, text=True, check=True)
    assert sh.stdout.strip() == computed
    (out / "IMAGE_VERSION").write_text("from-file\n")
    assert image_version(cfg) == "from-file"
    cfg.image_version = "from-env"
    assert image_version(cfg) == "from-env"
    env = FirecrackerConfig.from_env({"HARNESS_IMAGE_VERSION": "x", "HARNESS_VM_CACHE": "/c"})
    assert (env.image_version, env.vm_cache) == ("x", "/c")
    assert FirecrackerConfig.from_env({}).base_snapshot_dir == Path("/var/lib/secondbrain/vm/base")
    with pytest.raises(SandboxError, match="image version unknown"):
        image_version(FirecrackerConfig(rootfs=str(tmp_path / "nowhere" / "r.ext4")))


def test_cli_check_base(fake_fc, fake_mkfs, tmp_path, monkeypatch, capsys):
    cfg = _make_base(fake_fc, fake_mkfs, tmp_path)
    monkeypatch.setenv("HARNESS_FIRECRACKER_BIN", cfg.firecracker_bin)
    monkeypatch.setenv("HARNESS_KERNEL", cfg.kernel)
    monkeypatch.setenv("HARNESS_ROOTFS", cfg.rootfs)
    monkeypatch.setenv("HARNESS_IMAGE_VERSION", "img-1")
    monkeypatch.setenv("HARNESS_VM_CACHE", cfg.vm_cache)
    assert fc_main(["check-base"]) == 0
    assert json.loads(capsys.readouterr().out)["image_version"] == "img-1"
    monkeypatch.setenv("HARNESS_IMAGE_VERSION", "img-other")
    assert fc_main(["check-base"]) == 1
