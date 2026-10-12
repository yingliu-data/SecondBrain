"""sandbox/firecracker — one Firecracker microVM per session.

Boot from the read-only rootfs over the Firecracker API with a vsock device
and a per-session `/workspace` drive, stop, kill; full snapshots (`snapshot`),
restore from the shared BASE snapshot or from a per-session snapshot
(`restore`, `has_snapshot`). The base-snapshot build is sandbox/base_snapshot.py
(`python -m app.host.sandbox.base_snapshot build-base`, also reachable as
`python -m app.host.sandbox.firecracker build-base`, and
`harness/image/build_snapshot.py`). The jailer is still a follow-up.

Standard library, `protocol` and app/host/uds.py only (httpx-free, no
app.config): this module can move (an OpenShell runtime would be a second
backend behind the same methods), and `harness/image/build_snapshot.py` runs
it with nothing but agent-api/ and harness/ on sys.path.

Per-session files (all under the session directory):

    {session_dir}/vm/firecracker.sock   Firecracker API socket (host side only)
    {session_dir}/vm/workspace.img      ext4 image, /dev/vdb in the guest, /workspace
    {session_dir}/vm/rootfs.ext4        symlink -> FirecrackerConfig.rootfs (read-only)
    {session_dir}/vm/firecracker.log    Firecracker's own log
    {session_dir}/vm/metrics.json       Firecracker metrics (JSON lines, deltas per flush)
    {session_dir}/vm/console.log        the guest's ttyS0
    {session_dir}/vm/snapshot/          per-session snapshot: vmstate, mem, META.json
    {session_dir}/ipc/v.sock            vsock device uds_path. A guest-initiated
                                        connection to port P is forwarded to the
                                        host's listener at {session_dir}/ipc/v.sock_P
                                        -- the same names the test double uses.

Paths are relative to the Firecracker process's cwd ({session_dir}/vm): the
drives are `rootfs.ext4` and `workspace.img`, the vsock is `../ipc/v.sock`.
Firecracker records those strings in the snapshot's vmstate, so one BASE
snapshot (built in a scratch session dir) resolves to each session's own files
when it is loaded by a process whose cwd is that session's `vm/`. On load the
vsock path is also passed explicitly (`vsock_override`, Firecracker >= 1.16);
there is no drive override in the API (1.17), hence the relative names.

BASE snapshot, `HARNESS_VM_CACHE` (default /var/lib/secondbrain/vm):

    base/vmstate  base/mem  base/MANIFEST.json
        {format, image_version, kernel_sha256, rootfs_sha256, firecracker_version,
         guest_cid, vcpus, mem_mib, workspace_bytes, boot_args, created_ms}

Public API:
    SandboxError
    FirecrackerConfig            paths, sizes, boot args; `from_env()`
    FirecrackerSandbox(config)   start(session_dir), stop(), is_alive(),
                                 restore(session_dir, snapshot_dir=None),
                                 snapshot(snapshot_dir), has_snapshot(snapshot_dir=None);
                                 plus guest_cid, api_socket, vsock_uds, workspace_image,
                                 pid, returncode, wait(timeout),
                                 last_vm_event ({op, source, ms} for the `vm` host event)
    derive_guest_cid(name)       a per-session CID (>= 3) for cold boots
    instance_id(name)            Firecracker-legal `--id` for a session directory
    image_version(config)        the image identity a snapshot must match
    firecracker_version(bin)     "v1.17.0" from `firecracker --version` (cached)
    connect_unix, firecracker_path   re-exported from app.host.uds (sun_path's limit)
    API_ORDER, RESTORE_ORDER, SNAPSHOT_ORDER   the exact request sequences
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from protocol.clock import elapsed_ms, now_ms
from protocol.fsio import atomic_write, file_sha256
from protocol.ports import SOCKET_PREFIX, socket_name

from app.host.sandbox import Sandbox as _SandboxBase
from app.host.sandbox import SandboxError
from app.host.uds import UNIX_PATH_MAX, connect_unix, firecracker_path

__all__ = [
    "API_ORDER",
    "BASE_GUEST_CID",
    "DEFAULT_BOOT_ARGS",
    "DEFAULT_VM_CACHE",
    "RESTORE_ORDER",
    "SNAPSHOT_ORDER",
    "FirecrackerConfig",
    "FirecrackerSandbox",
    "SandboxError",
    "UNIX_PATH_MAX",
    "connect_unix",
    "derive_guest_cid",
    "firecracker_path",
    "firecracker_version",
    "image_version",
    "instance_id",
]

DEFAULT_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/harness-init"
# Firecracker appends `root=/dev/vda ro` itself for the drive marked is_root_device
# (read-only because the rootfs drive is), so the boot args carry no root=.
# The guest's `--idle-wait vsock --mount-workspace /dev/vdb` comes from
# harness-init, not from the boot args: a cold boot and the base-snapshot boot
# are the same boot.

# The configuration sequence `start()` sends. Tests assert this exact order.
API_ORDER = (
    ("PUT", "/machine-config"),
    ("PUT", "/boot-source"),
    ("PUT", "/drives/rootfs"),
    ("PUT", "/drives/workspace"),
    ("PUT", "/vsock"),
    ("PUT", "/actions"),
)
# `restore()`: a fresh process, nothing configured, one load (Firecracker
# allows LoadSnapshot only before any other configuration).
RESTORE_ORDER = (("PUT", "/snapshot/load"),)
# `snapshot()`: pause, then a full snapshot; the process is killed afterwards.
SNAPSHOT_ORDER = (("PATCH", "/vm"), ("PUT", "/snapshot/create"))

VSOCK_NAME = SOCKET_PREFIX  # "v.sock" under {session_dir}/ipc/; port P -> socket_name(P)
API_SOCK_NAME = "firecracker.sock"
WORKSPACE_IMG_NAME = "workspace.img"
ROOTFS_LINK_NAME = "rootfs.ext4"
LOG_NAME = "firecracker.log"
METRICS_NAME = "metrics.json"
CONSOLE_NAME = "console.log"

SNAPSHOT_STATE_NAME = "vmstate"
SNAPSHOT_MEM_NAME = "mem"
META_NAME = "META.json"             # per-session snapshot
MANIFEST_NAME = "MANIFEST.json"     # base snapshot
CONSUMED_SUFFIX = ".consumed"       # META.json after a successful restore
SNAPSHOT_FORMAT = 1
BASE_DIR_NAME = "base"
DEFAULT_VM_CACHE = "/var/lib/secondbrain/vm"
IMAGE_VERSION_FILE = "IMAGE_VERSION"   # next to the rootfs (harness/image/out/)

# vsock CIDs: 0, 1 (reserved), 2 (host). Guests get 3 .. 0xFFFFFFFE; we stay
# well under that so a CID is always a plain positive int in JSON.
MIN_GUEST_CID = 3
_CID_SPAN = 2**31 - MIN_GUEST_CID
# The base snapshot's CID. Firecracker's vsock is a userspace device backed by
# Unix sockets (not vhost-vsock), so the CID only matters inside the guest and
# every VM restored from the base shares it; load cannot change it.
BASE_GUEST_CID = 3

_INSTANCE_ID_RE = re.compile(r"[^A-Za-z0-9-]")
_VERSION_RE = re.compile(r"v\d+\.\d+\.\d+\S*")

# sun_path is 108 bytes on Linux. A session directory deep under data/sessions
# can push `{session_dir}/vm/firecracker.sock` past it, so the API socket path
# handed to Firecracker is made relative to its cwd ({session_dir}/vm) when the
# absolute form is too long (firecracker_path), and the host side connects
# through /proc/self/fd (connect_unix): app/host/uds.py.


def derive_guest_cid(name: str) -> int:
    """Per-session guest CID for a cold boot: >= 3, stable for a session name.

    Firecracker's vsock device is emulated in the VMM over Unix sockets, so
    the CID is not a host-wide resource; a stable per-session value only keeps
    logs readable. VMs restored from the base snapshot all carry
    BASE_GUEST_CID. The worker may override via `FirecrackerConfig.guest_cid`."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return MIN_GUEST_CID + int.from_bytes(digest[:8], "big") % _CID_SPAN


def instance_id(name: str) -> str:
    """Firecracker `--id`: alphanumerics and '-' only, 1..64 chars."""
    cleaned = _INSTANCE_ID_RE.sub("-", name).strip("-") or "session"
    return cleaned[:64]


@dataclass
class FirecrackerConfig:
    firecracker_bin: str = "firecracker"
    kernel: str = ""                 # vmlinux (uncompressed ELF), harness/image/out/vmlinux
    rootfs: str = ""                 # read-only ext4, harness/image/out/rootfs.ext4
    vcpus: int = 2
    mem_mib: int = 2048
    workspace_mib: int = 1024        # size of {session_dir}/vm/workspace.img when created
    workspace_template: str | None = None   # HARNESS_WORKSPACE_TEMPLATE: pre-formatted image
    mkfs_bin: str | None = None      # None -> shutil.which("mkfs.ext4")
    boot_args: str = DEFAULT_BOOT_ARGS
    guest_cid: int | None = None     # None -> derive_guest_cid(session dir name)
    log_path: str | None = None      # None -> {session_dir}/vm/firecracker.log
    metrics_path: str | None = None  # None -> {session_dir}/vm/metrics.json
    log_level: str = "Warning"
    api_timeout_s: float = 10.0      # per API request and for the socket to appear
    stop_grace_s: float = 5.0        # SendCtrlAltDel -> SIGKILL
    extra_args: list[str] = field(default_factory=list)
    # --- snapshots ---
    vm_cache: str = DEFAULT_VM_CACHE        # HARNESS_VM_CACHE: base/ lives here
    image_version: str | None = None        # HARNESS_IMAGE_VERSION; None -> image_version(cfg)
    firecracker_version: str | None = None  # HARNESS_FIRECRACKER_VERSION; None -> `--version`
    vsock_override: bool = True             # pass vsock_override on load (Firecracker >= 1.16)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "FirecrackerConfig":
        e = os.environ if env is None else env
        cfg = cls(
            firecracker_bin=e.get("HARNESS_FIRECRACKER_BIN", "firecracker"),
            kernel=e.get("HARNESS_KERNEL", ""),
            rootfs=e.get("HARNESS_ROOTFS", ""),
            vcpus=int(e.get("HARNESS_VM_VCPUS", 2)),
            mem_mib=int(e.get("HARNESS_VM_MEM_MIB", 2048)),
            workspace_mib=int(e.get("HARNESS_WORKSPACE_MIB", 1024)),
            workspace_template=e.get("HARNESS_WORKSPACE_TEMPLATE") or None,
            boot_args=e.get("HARNESS_BOOT_ARGS", DEFAULT_BOOT_ARGS),
            log_level=e.get("HARNESS_FIRECRACKER_LOG_LEVEL", "Warning"),
            vm_cache=e.get("HARNESS_VM_CACHE") or DEFAULT_VM_CACHE,
            image_version=e.get("HARNESS_IMAGE_VERSION") or None,
            firecracker_version=e.get("HARNESS_FIRECRACKER_VERSION") or None,
        )
        if e.get("HARNESS_GUEST_CID"):
            cfg.guest_cid = int(e["HARNESS_GUEST_CID"])
        return cfg

    def validate(self, *, restore: bool = False) -> None:
        """`restore=True`: a snapshot load needs no kernel (nothing boots)."""
        if not self.kernel and not restore:
            raise SandboxError("FirecrackerConfig.kernel is empty (HARNESS_KERNEL)")
        if not self.rootfs:
            raise SandboxError("FirecrackerConfig.rootfs is empty (HARNESS_ROOTFS)")
        if self.vcpus < 1 or self.mem_mib < 128 or self.workspace_mib < 1:
            raise SandboxError("vcpus >= 1, mem_mib >= 128, workspace_mib >= 1 required")
        if self.guest_cid is not None and self.guest_cid < MIN_GUEST_CID:
            raise SandboxError(f"guest_cid must be >= {MIN_GUEST_CID}")

    @property
    def base_snapshot_dir(self) -> Path:
        return Path(self.vm_cache) / BASE_DIR_NAME


# ------------------------------------------------------------ identities

_FC_VERSION_CACHE: dict[str, str] = {}
_SHA_CACHE: dict[tuple, str] = {}


def firecracker_version(binary: str) -> str:
    """`v1.17.0` from `firecracker --version` ("Firecracker v1.17.0"), cached
    per binary path. SandboxError when the binary cannot say."""
    if binary in _FC_VERSION_CACHE:
        return _FC_VERSION_CACHE[binary]
    try:
        res = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"cannot run {binary} --version: {exc}") from exc
    m = _VERSION_RE.search(res.stdout + res.stderr)
    if res.returncode != 0 or not m:
        raise SandboxError(f"{binary} --version: unexpected output {res.stdout!r}")
    _FC_VERSION_CACHE[binary] = m.group(0)
    return m.group(0)


def sha256_file(path: str | Path) -> str:
    """sha256 of a file, cached by (path, size, mtime) for the process."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        digest = file_sha256(p)
        if digest is None:
            raise OSError(f"cannot read {p}")
        _SHA_CACHE[key] = digest
    return _SHA_CACHE[key]


def image_version_from_shas(kernel_sha256: str, rootfs_sha256: str) -> str:
    """Same formula as harness/image/build.sh `version`:
    first 16 hex of sha256("<kernel sha>\\n<rootfs sha>\\n")."""
    blob = f"{kernel_sha256}\n{rootfs_sha256}\n".encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def image_version(config: FirecrackerConfig) -> str:
    """The image a snapshot must have been taken from. In order: the config /
    HARNESS_IMAGE_VERSION, the `IMAGE_VERSION` file next to the rootfs (written
    by `make image`), else computed from the kernel and rootfs sha256 (slow
    once per process: hashes ~350 MiB)."""
    if config.image_version:
        return config.image_version
    if config.rootfs:
        side = Path(config.rootfs).resolve().parent / IMAGE_VERSION_FILE
        try:
            text = side.read_text().strip()
            if text:
                return text
        except OSError:
            pass
    if config.kernel and config.rootfs and Path(config.kernel).is_file() and Path(config.rootfs).is_file():
        return image_version_from_shas(sha256_file(config.kernel), sha256_file(config.rootfs))
    raise SandboxError("image version unknown: set HARNESS_IMAGE_VERSION or put "
                       f"{IMAGE_VERSION_FILE} next to the rootfs")


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a Unix socket (the Firecracker API)."""

    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = connect_unix(self._path, self.timeout)


class FirecrackerSandbox(_SandboxBase):
    """One microVM. `start`/`restore`, `snapshot`, `stop`, `is_alive`,
    `has_snapshot` are the Sandbox interface; the rest is for the worker and
    the tests. A stopped or snapshotted sandbox may be started or restored
    again (a fresh Firecracker process on the same workspace.img).
    `config` defaults to the HARNESS_* environment (FirecrackerConfig.from_env)."""

    def __init__(self, config: FirecrackerConfig | None = None):
        self.config = config if config is not None else FirecrackerConfig.from_env()
        self.session_dir: Path | None = None
        self.vm_dir: Path | None = None
        self.api_socket: Path | None = None
        self.vsock_uds: Path | None = None
        self.workspace_image: Path | None = None
        self.log_path: Path | None = None
        self.metrics_path: Path | None = None
        self.instance_id: str | None = None
        self.guest_cid: int | None = None
        self._proc: subprocess.Popen | None = None
        self._last_returncode: int | None = None
        # {op, source, ms} for the worker's `vm` host event
        self.last_vm_event: dict | None = None
        # resolved once (the first snapshot check), not per check
        self._image_ver: str | None = None
        self._fc_ver: str | None = None

    # ------------------------------------------------------------ interface

    def start(self, session_dir) -> None:
        """Cold boot from the rootfs (the fallback when no snapshot)."""
        if self.is_alive():
            raise SandboxError("sandbox already started")
        self._proc = None
        self.config.validate()
        self._prepare(Path(session_dir))
        self.guest_cid = (
            self.config.guest_cid
            if self.config.guest_cid is not None
            else derive_guest_cid(self.session_dir.name)
        )
        self._ensure_workspace_image()
        self._link_rootfs()
        self._fresh_runtime_files()

        t0 = time.monotonic()
        self._launch()
        try:
            self._wait_for_api()
            self._configure()
        except BaseException:
            self._kill()
            raise
        self.last_vm_event = {"op": "boot", "source": "rootfs", "ms": _ms_since(t0)}

    def restore(self, session_dir, snapshot_dir=None) -> None:
        """Start from a snapshot: `snapshot_dir=None` -> the BASE snapshot in
        `{vm_cache}/base`, else a per-session snapshot. A new Firecracker
        process with cwd = {session_dir}/vm loads it and resumes the VM.
        SandboxError (before anything is launched) when the snapshot is
        missing or was taken from another image / Firecracker version; the
        worker then falls back to `start()`."""
        if self.is_alive():
            raise SandboxError("sandbox already started")
        self._proc = None
        self.config.validate(restore=True)
        snap, source = self._resolve_snapshot(snapshot_dir)
        meta = self._check_snapshot(snap, source)

        self._prepare(Path(session_dir))
        self.guest_cid = int(meta.get("guest_cid") or BASE_GUEST_CID)
        self._ensure_workspace_image()
        want = meta.get("workspace_bytes")
        if source == "base" and want and self.workspace_image.stat().st_size != int(want):
            # The guest's virtio-blk capacity is part of the vmstate; a
            # different-sized image would not mount. A rootfs boot reads it fresh.
            raise SandboxError(
                f"workspace.img is {self.workspace_image.stat().st_size} bytes, the base "
                f"snapshot was taken with {want} (rebuild the base or use start())")
        self._link_rootfs()
        self._fresh_runtime_files()

        body: dict = {
            "snapshot_path": str((snap / SNAPSHOT_STATE_NAME).absolute()),
            "mem_backend": {"backend_type": "File",
                            "backend_path": str((snap / SNAPSHOT_MEM_NAME).absolute())},
            "resume_vm": True,
        }
        if self.config.vsock_override:
            body["vsock_override"] = {"uds_path": self._vsock_rel()}

        t0 = time.monotonic()
        self._launch()
        try:
            self._wait_for_api()
            for method, path in RESTORE_ORDER:
                self._request(method, path, body)
        except BaseException:
            self._kill()
            raise
        self.last_vm_event = {"op": "restore", "source": source, "ms": _ms_since(t0)}
        if source == "session":
            # A session snapshot is single-use: the VM now runs on and changes
            # workspace.img, so restoring the same vmstate again would hand the
            # guest a page cache that no longer matches the disk.
            meta_path = snap / META_NAME
            try:
                os.replace(meta_path, meta_path.with_name(META_NAME + CONSUMED_SUFFIX))
            except OSError:
                pass

    def snapshot(self, snapshot_dir) -> None:
        """Pause the VM, write a full snapshot (vmstate + mem + META.json) into
        `snapshot_dir` (normally {session_dir}/vm/snapshot), then kill the
        process. Files are written to a sibling staging dir and swapped in, so
        a snapshot this VM was restored from is never truncated under it."""
        meta = {
            "format": SNAPSHOT_FORMAT,
            "image_version": self._image_version(),
            "firecracker_version": self._fc_version(),
            "guest_cid": self.guest_cid,
            "session": self.session_dir.name if self.session_dir else None,
        }
        self._snapshot_into(Path(snapshot_dir), META_NAME, meta, source="session")

    def has_snapshot(self, snapshot_dir=None) -> bool:
        try:
            self._check_snapshot(*self._resolve_snapshot(snapshot_dir))
        except SandboxError:
            return False
        return True

    def stop(self) -> None:
        """Graceful: SendCtrlAltDel (the guest init syncs /workspace and powers
        off, Firecracker exits); SIGKILL after `stop_grace_s`."""
        proc = self._proc
        if proc is None:
            return
        t0 = time.monotonic()
        if proc.poll() is None:
            try:
                self._request("PUT", "/actions", {"action_type": "SendCtrlAltDel"})
            except SandboxError:
                pass  # already gone or API dead: fall through to the kill
            try:
                proc.wait(timeout=self.config.stop_grace_s)
            except subprocess.TimeoutExpired:
                pass
        self._reap()
        self.last_vm_event = {"op": "destroy", "source": "session", "ms": _ms_since(t0)}

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def wait_exit(self, timeout: float) -> bool:
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    # --------------------------------------------------------------- extras

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        """Exit code of the current (if exited) or the last stopped VM process."""
        if self._proc is not None:
            return self._proc.returncode
        return self._last_returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block until the Firecracker process exits; returns its exit code."""
        if self._proc is None:
            raise SandboxError("sandbox not started")
        return self._proc.wait(timeout=timeout)

    def vsock_port_path(self, port: int) -> Path:
        """Host-side Unix socket that a guest connect(CID 2, port) lands on."""
        if self.vsock_uds is None:
            raise SandboxError("sandbox not started")
        return self.vsock_uds.with_name(socket_name(port))

    def flush_metrics(self) -> dict | None:
        """FlushMetrics, then the last JSON line of metrics.json (deltas since
        the previous flush), or None when there is none yet."""
        self._request("PUT", "/actions", {"action_type": "FlushMetrics"})
        try:
            lines = self.metrics_path.read_text().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except ValueError:
                    return None
        return None

    # ------------------------------------------------------------ internals

    def _prepare(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.vm_dir = session_dir / "vm"
        ipc_dir = session_dir / "ipc"
        self.vm_dir.mkdir(parents=True, exist_ok=True)
        ipc_dir.mkdir(parents=True, exist_ok=True)
        self.api_socket = self.vm_dir / API_SOCK_NAME
        self.vsock_uds = ipc_dir / VSOCK_NAME
        self.workspace_image = self.vm_dir / WORKSPACE_IMG_NAME
        self.log_path = Path(self.config.log_path) if self.config.log_path else self.vm_dir / LOG_NAME
        self.metrics_path = (
            Path(self.config.metrics_path) if self.config.metrics_path else self.vm_dir / METRICS_NAME
        )
        self.instance_id = instance_id(session_dir.name)

    def _fresh_runtime_files(self) -> None:
        # Firecracker binds both sockets itself; stale files make it fail.
        _unlink(self.api_socket)
        _unlink(self.vsock_uds)
        self.log_path.touch(exist_ok=True)
        self.metrics_path.touch(exist_ok=True)

    def _vsock_rel(self) -> str:
        """`../ipc/v.sock`: relative to the Firecracker cwd, recorded as such
        in snapshots, so the base snapshot resolves per session."""
        return os.path.relpath(str(self.vsock_uds), str(self.vm_dir))

    def _link_rootfs(self) -> None:
        """{vm}/rootfs.ext4 -> the configured rootfs: the drive path recorded
        in a snapshot is the relative name, wherever the image lives."""
        link = self.vm_dir / ROOTFS_LINK_NAME
        target = str(Path(self.config.rootfs).absolute())
        try:
            if link.is_symlink() and os.readlink(link) == target:
                return
        except OSError:
            pass
        _unlink(link)
        os.symlink(target, link)

    def _fc_version(self) -> str:
        if self._fc_ver is None:
            self._fc_ver = (self.config.firecracker_version
                            or firecracker_version(self.config.firecracker_bin))
        return self._fc_ver

    def _image_version(self) -> str:
        if self._image_ver is None:
            self._image_ver = image_version(self.config)
        return self._image_ver

    def _resolve_snapshot(self, snapshot_dir) -> tuple[Path, str]:
        """(snapshot dir, source): None -> the BASE snapshot, else a session one."""
        if snapshot_dir is None:
            return self.config.base_snapshot_dir, "base"
        return Path(snapshot_dir), "session"

    def _check_snapshot(self, snap: Path, source: str) -> dict:
        meta_name = MANIFEST_NAME if source == "base" else META_NAME
        for name in (SNAPSHOT_STATE_NAME, SNAPSHOT_MEM_NAME):
            p = snap / name
            if not p.is_file() or p.stat().st_size == 0:
                raise SandboxError(f"no {source} snapshot: {p} missing")
        try:
            meta = json.loads((snap / meta_name).read_text())
        except (OSError, ValueError) as exc:
            raise SandboxError(f"no {source} snapshot: {snap / meta_name}: {exc}") from exc
        if not isinstance(meta, dict):
            raise SandboxError(f"{snap / meta_name} is not an object")
        if meta.get("format", SNAPSHOT_FORMAT) != SNAPSHOT_FORMAT:
            raise SandboxError(f"{source} snapshot format {meta.get('format')} != {SNAPSHOT_FORMAT}")
        want_img = self._image_version()
        if meta.get("image_version") != want_img:
            raise SandboxError(
                f"{source} snapshot image_version {meta.get('image_version')!r} != "
                f"current {want_img!r} (rebuild with `make snapshot`)")
        want_fc = self._fc_version()
        if meta.get("firecracker_version") != want_fc:
            raise SandboxError(
                f"{source} snapshot firecracker_version {meta.get('firecracker_version')!r} "
                f"!= current {want_fc!r}")
        return meta

    def _snapshot_into(self, dest: Path, meta_name: str, meta: dict, *, source: str) -> None:
        if not self.is_alive():
            raise SandboxError("snapshot: the VM is not running")
        t0 = time.monotonic()
        staging = dest.parent / f".{dest.name}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        bodies = {
            "/vm": {"state": "Paused"},
            "/snapshot/create": {
                "snapshot_type": "Full",
                # relative to the Firecracker cwd ({session_dir}/vm)
                "snapshot_path": os.path.relpath(str(staging / SNAPSHOT_STATE_NAME), str(self.vm_dir)),
                "mem_file_path": os.path.relpath(str(staging / SNAPSHOT_MEM_NAME), str(self.vm_dir)),
            },
        }
        try:
            for method, path in SNAPSHOT_ORDER:
                self._request(method, path, bodies[path])
        except SandboxError:
            try:  # leave a usable VM behind if only the create failed
                self._request("PATCH", "/vm", {"state": "Resumed"})
            except SandboxError:
                pass
            shutil.rmtree(staging, ignore_errors=True)
            raise
        meta = dict(meta, created_ms=now_ms())
        write_json(staging / meta_name, meta)
        # The VM is paused and its block files fsynced by Firecracker: kill it.
        self._reap()
        _swap_dir(staging, dest)
        self.last_vm_event = {"op": "snapshot", "source": source, "ms": _ms_since(t0)}

    def _ensure_workspace_image(self) -> None:
        img = self.workspace_image
        assert img is not None
        if img.exists() and img.stat().st_size > 0:
            return
        mkfs = self.config.mkfs_bin or shutil.which("mkfs.ext4")
        if mkfs:
            tmp = img.with_suffix(".img.tmp")
            _unlink(tmp)
            with open(tmp, "wb") as fh:
                fh.truncate(self.config.workspace_mib * 1024 * 1024)   # sparse
            res = subprocess.run(
                [mkfs, "-F", "-q", "-L", "workspace", str(tmp)],
                capture_output=True, text=True,
            )
            if res.returncode != 0:
                _unlink(tmp)
                raise SandboxError(f"mkfs.ext4 failed ({res.returncode}): {res.stderr.strip()}")
            os.replace(tmp, img)
            return
        template = self.config.workspace_template
        if template and Path(template).is_file():
            tmp = img.with_suffix(".img.tmp")
            _copy_sparse(Path(template), tmp)
            os.replace(tmp, img)
            return
        raise SandboxError(
            "no mkfs.ext4 on PATH and no workspace template "
            "(set HARNESS_WORKSPACE_TEMPLATE to a pre-formatted ext4 image)"
        )

    def _launch(self) -> None:
        assert self.vm_dir is not None and self.api_socket is not None
        argv = [
            self.config.firecracker_bin,
            "--api-sock", firecracker_path(self.api_socket, self.vm_dir),
            "--id", self.instance_id or "session",
            "--log-path", str(self.log_path),
            "--level", self.config.log_level,
            "--metrics-path", str(self.metrics_path),
            *self.config.extra_args,
        ]
        console = open(self.vm_dir / CONSOLE_NAME, "ab")
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=console,         # the guest's ttyS0 (boot messages, init output)
                stderr=subprocess.STDOUT,
                cwd=str(self.vm_dir),   # relative drive / vsock / snapshot paths resolve here
                start_new_session=True,  # our SIGKILL never reaches the worker
            )
        except OSError as exc:
            raise SandboxError(f"cannot launch {self.config.firecracker_bin}: {exc}") from exc
        finally:
            console.close()

    def _wait_for_api(self) -> None:
        assert self._proc is not None and self.api_socket is not None
        deadline = time.monotonic() + self.config.api_timeout_s
        while True:
            if self._proc.poll() is not None:
                raise SandboxError(
                    f"firecracker exited with {self._proc.returncode} before the API came up "
                    f"(see {self.vm_dir / CONSOLE_NAME})"
                )
            if self.api_socket.exists():
                try:
                    connect_unix(self.api_socket, 0.5).close()
                    return
                except OSError:
                    pass
            if time.monotonic() > deadline:
                raise SandboxError(f"firecracker API socket {self.api_socket} not ready")
            time.sleep(0.002)

    def _configure(self) -> None:
        cfg = self.config
        bodies = {
            "/machine-config": {
                "vcpu_count": cfg.vcpus,
                "mem_size_mib": cfg.mem_mib,
                "smt": False,
            },
            "/boot-source": {
                "kernel_image_path": str(Path(cfg.kernel)),
                "boot_args": cfg.boot_args,
            },
            "/drives/rootfs": {
                "drive_id": "rootfs",
                "path_on_host": ROOTFS_LINK_NAME,      # symlink in the cwd
                "is_root_device": True,
                "is_read_only": True,
            },
            "/drives/workspace": {
                "drive_id": "workspace",
                "path_on_host": WORKSPACE_IMG_NAME,    # relative to the cwd
                "is_root_device": False,
                "is_read_only": False,
            },
            "/vsock": {
                "guest_cid": self.guest_cid,
                "uds_path": self._vsock_rel(),
            },
            "/actions": {"action_type": "InstanceStart"},
        }
        for method, path in API_ORDER:
            self._request(method, path, bodies[path])

    def _request(self, method: str, path: str, body: dict | None = None) -> str:
        assert self.api_socket is not None
        payload = json.dumps(body) if body is not None else None
        conn = _UnixHTTPConnection(self.api_socket, self.config.api_timeout_s)
        try:
            conn.request(
                method, path, body=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            if resp.status >= 400:
                raise SandboxError(
                    f"{method} {path} -> {resp.status}: {text}", status=resp.status, body=text
                )
            return text
        except (OSError, http.client.HTTPException) as exc:
            raise SandboxError(f"{method} {path} failed: {exc}") from exc
        finally:
            conn.close()

    def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _reap(self) -> None:
        """Kill the process if it still runs, keep its exit code, forget it,
        remove its sockets (after stop, and after a snapshot)."""
        proc = self._proc
        self._kill()
        self._last_returncode = proc.returncode if proc is not None else None
        self._proc = None
        self._cleanup_sockets()

    def _cleanup_sockets(self) -> None:
        _unlink(self.api_socket)
        _unlink(self.vsock_uds)


# ------------------------------------------------------------- helpers

def _ms_since(t0: float) -> float:
    return elapsed_ms(t0, 1)


def write_json(path: Path, obj: dict) -> None:
    """A snapshot's META.json / MANIFEST.json (and LocalSandbox's marker).
    durable=False, as before: the snapshot directory is swapped in by rename
    only after the VM's own files, and a META.json lost to a power cut only
    makes the snapshot unusable (restore falls back), never wrong. 0644."""
    atomic_write(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode(),
                 durable=False, mode=0o644)


def _swap_dir(staging: Path, dest: Path) -> None:
    """Replace `dest` with `staging`. A running VM mapping the old `mem` keeps
    its inode; nothing is truncated in place."""
    old = dest.parent / f".{dest.name}.old"
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    os.replace(staging, dest)
    shutil.rmtree(old, ignore_errors=True)


def _copy_sparse(src: Path, dst: Path, chunk: int = 1 << 20) -> None:
    """Copy a file keeping its holes (shutil.copyfile would fill them)."""
    with open(src, "rb") as sf, open(dst, "wb") as df:
        size = os.fstat(sf.fileno()).st_size
        df.truncate(size)
        pos = 0
        while pos < size:
            try:
                data = os.lseek(sf.fileno(), pos, os.SEEK_DATA)
            except OSError:          # ENXIO: only holes remain
                break
            hole = os.lseek(sf.fileno(), data, os.SEEK_HOLE)
            sf.seek(data)
            df.seek(data)
            remaining = hole - data
            while remaining > 0:
                buf = sf.read(min(chunk, remaining))
                if not buf:
                    break
                df.write(buf)
                remaining -= len(buf)
            pos = hole


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":  # pragma: no cover - the old entry point
    from app.host.sandbox.base_snapshot import main
    sys.exit(main())
