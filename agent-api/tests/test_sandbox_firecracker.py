"""FirecrackerSandbox against a fake Firecracker: a Python script that takes the
real CLI (`--api-sock PATH --id ID ...`), serves an http.server on that Unix
socket, records every request as JSONL, and exits on SendCtrlAltDel like the
real binary does when the guest powers off. No KVM involved.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

from app.host.sandbox.firecracker import (
    API_ORDER,
    DEFAULT_BOOT_ARGS,
    UNIX_PATH_MAX,
    FirecrackerConfig,
    FirecrackerSandbox,
    SandboxError,
    connect_unix,
    derive_guest_cid,
    firecracker_path,
    instance_id,
)

FAKE_FIRECRACKER = r'''"""Fake Firecracker. Env: FAKE_FC_RECORD (JSONL of requests), FAKE_FC_FAIL_PATH
(path that answers 400), FAKE_FC_IGNORE_CAD=1 (never exit on SendCtrlAltDel)."""
import json, os, socket, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
opts = {}
i = 0
while i < len(args):
    if args[i].startswith("--") and i + 1 < len(args) and not args[i + 1].startswith("--"):
        opts[args[i][2:]] = args[i + 1]; i += 2
    else:
        opts[args[i][2:]] = True; i += 1
api_sock = opts["api-sock"]
record = os.environ["FAKE_FC_RECORD"]
fail_path = os.environ.get("FAKE_FC_FAIL_PATH")
with open(record, "a") as fh:
    fh.write(json.dumps({"argv": args}) + "\n")


class UnixHTTPServer(HTTPServer):
    address_family = socket.AF_UNIX
    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_name = "localhost"; self.server_port = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try: return json.loads(raw) if raw else None
        except ValueError: return raw.decode()
    def _reply(self, status, payload=b""):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload: self.wfile.write(payload)
    def do_PUT(self):
        body = self._body()
        with open(record, "a") as fh:
            fh.write(json.dumps({"method": "PUT", "path": self.path, "body": body}) + "\n")
        if fail_path and self.path == fail_path:
            self._reply(400, json.dumps({"fault_message": "fake rejected " + self.path}).encode())
            return
        self._reply(204)
        if self.path == "/actions" and isinstance(body, dict) \
                and body.get("action_type") == "SendCtrlAltDel" \
                and not os.environ.get("FAKE_FC_IGNORE_CAD"):
            threading.Timer(0.1, lambda: os._exit(0)).start()
    def do_GET(self):
        with open(record, "a") as fh:
            fh.write(json.dumps({"method": "GET", "path": self.path, "body": None}) + "\n")
        self._reply(200, json.dumps({"id": opts.get("id"), "state": "Running",
                                     "vmm_version": "fake", "app_name": "Firecracker"}).encode())
    do_PATCH = do_PUT


UnixHTTPServer(api_sock, Handler).serve_forever()
'''


@pytest.fixture
def fake_fc(tmp_path, monkeypatch):
    """Returns (binary path, record path). Requests are appended to the record."""
    binary = tmp_path / "bin" / "firecracker"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\n" + FAKE_FIRECRACKER)   # absolute: PATH-free
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    record = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_FC_RECORD", str(record))
    monkeypatch.delenv("FAKE_FC_FAIL_PATH", raising=False)
    monkeypatch.delenv("FAKE_FC_IGNORE_CAD", raising=False)
    return binary, record


@pytest.fixture
def fake_mkfs(tmp_path):
    """A stand-in mkfs.ext4 that stamps the image, so the test never needs e2fsprogs."""
    mkfs = tmp_path / "bin" / "mkfs.ext4"
    mkfs.parent.mkdir(exist_ok=True)
    mkfs.write_text('#!/bin/sh\nfor last; do :; done\nprintf "FAKE-EXT4" > "$last"\n')
    mkfs.chmod(mkfs.stat().st_mode | stat.S_IXUSR)
    return mkfs


@pytest.fixture
def session_dir(tmp_path):
    d = tmp_path / "sessions" / "owner" / "default_abc123"
    d.mkdir(parents=True)
    return d


def _config(fake_fc, fake_mkfs, tmp_path, **over) -> FirecrackerConfig:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.write_bytes(b"ELF")
    rootfs.write_bytes(b"ext4")
    cfg = FirecrackerConfig(
        firecracker_bin=str(fake_fc[0]),
        kernel=str(kernel),
        rootfs=str(rootfs),
        mkfs_bin=str(fake_mkfs),
        workspace_mib=8,
        stop_grace_s=1.0,
        api_timeout_s=5.0,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _requests(record: Path) -> list[dict]:
    return [json.loads(l) for l in record.read_text().splitlines() if '"method"' in l]


def _argv(record: Path) -> list[str]:
    return json.loads(record.read_text().splitlines()[0])["argv"]


def _from_vm_dir(session_dir: Path, given: str) -> str:
    """Firecracker resolves relative socket paths against its cwd, {session_dir}/vm."""
    return os.path.normpath(os.path.join(session_dir / "vm", given))


def _wait_exit(sb: FirecrackerSandbox, timeout=3.0):
    deadline = time.monotonic() + timeout
    while sb.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)


# ---------------------------------------------------------------- start()


def test_start_sends_the_documented_sequence(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    try:
        sb.start(session_dir)
        assert sb.is_alive()
        reqs = _requests(fake_fc[1])
        assert [(r["method"], r["path"]) for r in reqs] == list(API_ORDER)

        by_path = {r["path"]: r["body"] for r in reqs}
        assert by_path["/machine-config"] == {"vcpu_count": 2, "mem_size_mib": 2048, "smt": False}
        assert by_path["/boot-source"] == {
            "kernel_image_path": str(tmp_path / "vmlinux"),
            "boot_args": DEFAULT_BOOT_ARGS,
        }
        assert "init=/sbin/harness-init" in by_path["/boot-source"]["boot_args"]
        assert "root=" not in by_path["/boot-source"]["boot_args"]  # Firecracker adds it

        rootfs = by_path["/drives/rootfs"]
        assert rootfs["drive_id"] == "rootfs"
        assert rootfs["is_root_device"] is True
        assert rootfs["is_read_only"] is True
        assert rootfs["path_on_host"] == str(tmp_path / "rootfs.ext4")

        ws = by_path["/drives/workspace"]
        assert ws == {
            "drive_id": "workspace",
            "path_on_host": str(session_dir / "vm" / "workspace.img"),
            "is_root_device": False,
            "is_read_only": False,
        }

        vsock = by_path["/vsock"]
        assert _from_vm_dir(session_dir, vsock["uds_path"]) == str(session_dir / "ipc" / "v.sock")
        assert vsock["guest_cid"] == derive_guest_cid("default_abc123")
        assert vsock["guest_cid"] >= 3
        # A guest connect(CID 2, port P) is forwarded to {uds_path}_P on the host.
        assert sb.vsock_port_path(5000) == session_dir / "ipc" / "v.sock_5000"
        assert sb.vsock_port_path(5010) == session_dir / "ipc" / "v.sock_5010"

        assert by_path["/actions"] == {"action_type": "InstanceStart"}
    finally:
        sb.stop()


def test_launch_argv_and_session_files(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    try:
        sb.start(session_dir)
        argv = _argv(fake_fc[1])
        assert argv[0] == "--api-sock"
        assert _from_vm_dir(session_dir, argv[1]) == str(session_dir / "vm" / "firecracker.sock")
        assert argv[2:4] == ["--id", "default-abc123"]      # '_' is not legal in an id
        assert argv[argv.index("--log-path") + 1] == str(session_dir / "vm" / "firecracker.log")
        assert argv[argv.index("--metrics-path") + 1] == str(session_dir / "vm" / "metrics.json")
        assert (session_dir / "vm").is_dir()
        assert (session_dir / "ipc").is_dir()
        assert (session_dir / "vm" / "firecracker.log").exists()
        assert (session_dir / "vm" / "metrics.json").exists()
        assert sb.api_socket == session_dir / "vm" / "firecracker.sock"
        assert sb.vsock_uds == session_dir / "ipc" / "v.sock"
        assert sb.pid is not None
    finally:
        sb.stop()


def test_workspace_image_created_once_with_mkfs(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    img = session_dir / "vm" / "workspace.img"
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    sb.stop()
    assert img.read_bytes() == b"FAKE-EXT4"
    img.write_bytes(b"USER DATA")
    sb2 = FirecrackerSandbox(cfg)
    sb2.start(session_dir)
    sb2.stop()
    assert img.read_bytes() == b"USER DATA"       # an existing image is kept


def test_workspace_template_fallback_without_mkfs(fake_fc, fake_mkfs, session_dir, tmp_path):
    template = tmp_path / "workspace-template.img"
    with open(template, "wb") as fh:          # sparse: data, 8 MiB hole, data
        fh.write(b"TEMPLATE-EXT4")
        fh.seek(8 * 1024 * 1024)
        fh.write(b"TAIL")
    cfg = _config(fake_fc, fake_mkfs, tmp_path, workspace_template=str(template))
    cfg.mkfs_bin = None          # probe PATH, which we make empty below
    env_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(tmp_path / "empty-path")
    try:
        assert shutil.which("mkfs.ext4") is None
        sb = FirecrackerSandbox(cfg)
        sb.start(session_dir)
        sb.stop()
    finally:
        os.environ["PATH"] = env_path
    img = session_dir / "vm" / "workspace.img"
    assert img.read_bytes() == template.read_bytes()
    assert img.stat().st_size == 8 * 1024 * 1024 + 4
    assert img.stat().st_blocks * 512 < 1024 * 1024        # the hole was kept


def test_no_mkfs_and_no_template_is_an_error(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    cfg.mkfs_bin = None
    env_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(tmp_path / "empty-path")
    try:
        with pytest.raises(SandboxError, match="mkfs.ext4"):
            FirecrackerSandbox(cfg).start(session_dir)
    finally:
        os.environ["PATH"] = env_path


@pytest.mark.skipif(shutil.which("mkfs.ext4") is None, reason="e2fsprogs not installed")
def test_real_mkfs_makes_a_sparse_ext4_image(fake_fc, session_dir, tmp_path):
    kernel = tmp_path / "vmlinux"; kernel.write_bytes(b"ELF")
    rootfs = tmp_path / "rootfs.ext4"; rootfs.write_bytes(b"ext4")
    cfg = FirecrackerConfig(firecracker_bin=str(fake_fc[0]), kernel=str(kernel),
                            rootfs=str(rootfs), workspace_mib=16, stop_grace_s=1.0)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    sb.stop()
    img = session_dir / "vm" / "workspace.img"
    st = img.stat()
    assert st.st_size == 16 * 1024 * 1024
    assert st.st_blocks * 512 < st.st_size          # sparse
    with open(img, "rb") as fh:
        fh.seek(0x438)
        assert fh.read(2) == b"\x53\xef"             # ext4 superblock magic


def test_short_session_dir_gets_absolute_socket_paths(fake_fc, fake_mkfs, tmp_path):
    """With a path that fits sun_path, Firecracker receives absolute paths:
    `{session_dir}/ipc/v.sock` and `{session_dir}/vm/firecracker.sock`."""
    with tempfile.TemporaryDirectory(prefix="fc-", dir="/tmp") as short:
        session_dir = Path(short) / "default_s1"
        session_dir.mkdir()
        assert len(str(session_dir / "ipc" / "v.sock_5010")) <= UNIX_PATH_MAX
        cfg = _config(fake_fc, fake_mkfs, tmp_path)
        sb = FirecrackerSandbox(cfg)
        try:
            sb.start(session_dir)
            argv = _argv(fake_fc[1])
            assert argv[1] == str(session_dir / "vm" / "firecracker.sock")
            vsock = [r for r in _requests(fake_fc[1]) if r["path"] == "/vsock"][0]["body"]
            assert vsock["uds_path"] == str(session_dir / "ipc" / "v.sock")
        finally:
            sb.stop()


def test_long_session_dir_uses_vm_relative_paths(fake_fc, fake_mkfs, tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "default_long"
    deep.mkdir(parents=True)
    assert not len(str(deep / "vm" / "firecracker.sock")) <= UNIX_PATH_MAX
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    try:
        sb.start(deep)                       # host connects via /proc/self/fd
        argv = _argv(fake_fc[1])
        assert argv[1] == "firecracker.sock"
        vsock = [r for r in _requests(fake_fc[1]) if r["path"] == "/vsock"][0]["body"]
        assert vsock["uds_path"] == os.path.join("..", "ipc", "v.sock")
        # connect_unix works on the long absolute path too
        connect_unix(deep / "vm" / "firecracker.sock", 1.0).close()
    finally:
        sb.stop()
    assert firecracker_path(Path("/short/v.sock"), Path("/short/vm")) == "/short/v.sock"


# ----------------------------------------------------------------- errors


def test_api_400_raises_sandbox_error_with_body_and_kills(fake_fc, fake_mkfs, session_dir,
                                                          tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FC_FAIL_PATH", "/vsock")
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    with pytest.raises(SandboxError) as info:
        sb.start(session_dir)
    err = info.value
    assert err.status == 400
    assert "fake rejected /vsock" in err.body
    assert "fake rejected /vsock" in str(err)
    _wait_exit(sb)
    assert not sb.is_alive()
    paths = [r["path"] for r in _requests(fake_fc[1])]
    assert paths[-1] == "/vsock"                      # nothing after the failure
    assert "/actions" not in paths


def test_missing_binary_is_a_sandbox_error(fake_mkfs, session_dir, tmp_path):
    kernel = tmp_path / "vmlinux"; kernel.write_bytes(b"ELF")
    rootfs = tmp_path / "rootfs.ext4"; rootfs.write_bytes(b"ext4")
    cfg = FirecrackerConfig(firecracker_bin=str(tmp_path / "nope"), kernel=str(kernel),
                            rootfs=str(rootfs), mkfs_bin=str(fake_mkfs))
    sb = FirecrackerSandbox(cfg)
    with pytest.raises(SandboxError, match="cannot launch"):
        sb.start(session_dir)
    assert not sb.is_alive()


def test_binary_that_dies_before_the_api(fake_mkfs, session_dir, tmp_path):
    bad = tmp_path / "bin" / "firecracker-dies"
    bad.write_text("#!/bin/sh\nexit 7\n")
    bad.chmod(bad.stat().st_mode | stat.S_IXUSR)
    kernel = tmp_path / "vmlinux"; kernel.write_bytes(b"ELF")
    rootfs = tmp_path / "rootfs.ext4"; rootfs.write_bytes(b"ext4")
    cfg = FirecrackerConfig(firecracker_bin=str(bad), kernel=str(kernel), rootfs=str(rootfs),
                            mkfs_bin=str(fake_mkfs), api_timeout_s=2.0)
    with pytest.raises(SandboxError, match="exited with 7"):
        FirecrackerSandbox(cfg).start(session_dir)


def test_config_validation():
    with pytest.raises(SandboxError, match="kernel"):
        FirecrackerConfig(rootfs="r").validate()
    with pytest.raises(SandboxError, match="rootfs"):
        FirecrackerConfig(kernel="k").validate()
    with pytest.raises(SandboxError, match="guest_cid"):
        FirecrackerConfig(kernel="k", rootfs="r", guest_cid=2).validate()
    FirecrackerConfig(kernel="k", rootfs="r", guest_cid=3).validate()


# ------------------------------------------------------------------ stop()


def test_stop_sends_ctrl_alt_del_and_waits(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    t0 = time.monotonic()
    sb.stop()
    assert time.monotonic() - t0 < cfg.stop_grace_s     # exited on CAD, no SIGKILL wait
    assert not sb.is_alive()
    assert sb.returncode == 0
    actions = [r["body"] for r in _requests(fake_fc[1]) if r["path"] == "/actions"]
    assert actions == [{"action_type": "InstanceStart"}, {"action_type": "SendCtrlAltDel"}]
    assert not (session_dir / "vm" / "firecracker.sock").exists()
    assert not (session_dir / "ipc" / "v.sock").exists()
    sb.stop()                                            # idempotent


def test_stop_sigkills_after_grace(fake_fc, fake_mkfs, session_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FC_IGNORE_CAD", "1")
    cfg = _config(fake_fc, fake_mkfs, tmp_path, stop_grace_s=0.5)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    t0 = time.monotonic()
    sb.stop()
    elapsed = time.monotonic() - t0
    assert 0.5 <= elapsed < 5
    assert not sb.is_alive()
    assert sb.returncode == -9


def test_restart_after_stop_is_a_fresh_vm(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    first_pid = sb.pid
    sb.stop()
    sb.start(session_dir)
    try:
        assert sb.is_alive() and sb.pid != first_pid
        starts = [r for r in _requests(fake_fc[1]) if r["body"] == {"action_type": "InstanceStart"}]
        assert len(starts) == 2
    finally:
        sb.stop()


def test_default_config_comes_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_KERNEL", "/k")
    monkeypatch.setenv("HARNESS_ROOTFS", "/r")
    sb = FirecrackerSandbox()
    assert (sb.config.kernel, sb.config.rootfs) == ("/k", "/r")
    from app.host.sandbox import Sandbox
    assert isinstance(sb, Sandbox)


def test_stop_before_start_is_a_noop():
    sb = FirecrackerSandbox(FirecrackerConfig(kernel="k", rootfs="r"))
    sb.stop()
    assert not sb.is_alive()


def test_start_twice_is_an_error(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    try:
        with pytest.raises(SandboxError, match="already started"):
            sb.start(session_dir)
    finally:
        sb.stop()


# ------------------------------------------------------------- small bits


def test_guest_cid_override_and_derivation(fake_fc, fake_mkfs, session_dir, tmp_path):
    cfg = _config(fake_fc, fake_mkfs, tmp_path, guest_cid=42)
    sb = FirecrackerSandbox(cfg)
    sb.start(session_dir)
    sb.stop()
    vsock = [r for r in _requests(fake_fc[1]) if r["path"] == "/vsock"][0]["body"]
    assert vsock["guest_cid"] == 42
    assert derive_guest_cid("a") == derive_guest_cid("a")
    assert derive_guest_cid("a") != derive_guest_cid("b")
    assert all(derive_guest_cid(f"s{i}") >= 3 for i in range(200))


def test_instance_id_rules():
    assert instance_id("default_abc123") == "default-abc123"
    assert instance_id("tenant one/x.y") == "tenant-one-x-y"
    assert instance_id("___") == "session"
    assert len(instance_id("a" * 100)) == 64


def test_from_env():
    env = {
        "HARNESS_FIRECRACKER_BIN": "/opt/fc/firecracker",
        "HARNESS_KERNEL": "/vm/vmlinux",
        "HARNESS_ROOTFS": "/vm/rootfs.ext4",
        "HARNESS_VM_VCPUS": "4",
        "HARNESS_VM_MEM_MIB": "4096",
        "HARNESS_WORKSPACE_MIB": "2048",
        "HARNESS_WORKSPACE_TEMPLATE": "/vm/workspace-template.img",
    }
    cfg = FirecrackerConfig.from_env(env)
    assert cfg.firecracker_bin == "/opt/fc/firecracker"
    assert cfg.kernel == "/vm/vmlinux"
    assert cfg.rootfs == "/vm/rootfs.ext4"
    assert (cfg.vcpus, cfg.mem_mib, cfg.workspace_mib) == (4, 4096, 2048)
    assert cfg.workspace_template == "/vm/workspace-template.img"
    assert cfg.boot_args == DEFAULT_BOOT_ARGS
    assert cfg.guest_cid is None


def test_module_is_httpx_free():
    import app.host.sandbox.firecracker as mod
    src = Path(mod.__file__).read_text()
    assert "httpx" not in src.replace("httpx-free", "")
    assert "import requests" not in src
