"""sandbox/firecracker — one Firecracker microVM per session (PLAN.md D6a, D18, M1).

M1 scope: boot from the read-only rootfs over the Firecracker API with a vsock
device and a per-session `/workspace` drive, stop, kill. Snapshots, restore and
the jailer are M3 follow-ups (docs/RUNBOOK-M1.md lists them).

Standard library plus `subprocess` only: this module must stay httpx-free so it
can move (an OpenShell runtime would be a second backend behind the same three
methods, PLAN.md section 11).

Per-session files (all under the session directory, PLAN.md section 3):

    {session_dir}/vm/firecracker.sock   Firecracker API socket (host side only)
    {session_dir}/vm/workspace.img      ext4 image, /dev/vdb in the guest, /workspace
    {session_dir}/vm/firecracker.log    Firecracker's own log
    {session_dir}/vm/metrics.json       Firecracker metrics (JSON lines)
    {session_dir}/ipc/v.sock            vsock device uds_path. Firecracker binds it
                                        for host-initiated connections (CONNECT port).
                                        A guest-initiated connection to port P is
                                        forwarded to the host's listener at
                                        {session_dir}/ipc/v.sock_P -- the same names
                                        the test double (guest.transport.UnixConnector)
                                        uses, so endpoints.py serves both unchanged.

Public API:
    SandboxError
    FirecrackerConfig            paths, sizes, boot args; `from_env()`
    FirecrackerSandbox(config)   start(session_dir), stop(), is_alive(); plus
                                 guest_cid, api_socket, vsock_uds, workspace_image,
                                 pid, returncode, wait(timeout)
    derive_guest_cid(name)       the default per-session CID (>= 3)
    instance_id(name)            Firecracker-legal `--id` for a session directory
    connect_unix(path, timeout)  connect() that survives sun_path's 108-byte limit;
                                 endpoints.py can use it for the v.sock_P listeners
    firecracker_path(path, cwd)  the socket path form Firecracker is given
    API_ORDER                    the exact configuration sequence, for tests and docs
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

try:  # the worker's interface (owned elsewhere); standalone when absent
    from app.host.sandbox import Sandbox as _SandboxBase
except ImportError:  # pragma: no cover
    _SandboxBase = object  # type: ignore[assignment,misc]

__all__ = [
    "API_ORDER",
    "DEFAULT_BOOT_ARGS",
    "FirecrackerConfig",
    "FirecrackerSandbox",
    "SandboxError",
    "UNIX_PATH_MAX",
    "connect_unix",
    "derive_guest_cid",
    "firecracker_path",
    "instance_id",
]

DEFAULT_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/harness-init"
# Firecracker appends `root=/dev/vda ro` itself for the drive marked is_root_device
# (read-only because the rootfs drive is), so the boot args carry no root=.

# The configuration sequence `start()` sends. Tests assert this exact order.
API_ORDER = (
    ("PUT", "/machine-config"),
    ("PUT", "/boot-source"),
    ("PUT", "/drives/rootfs"),
    ("PUT", "/drives/workspace"),
    ("PUT", "/vsock"),
    ("PUT", "/actions"),
)

VSOCK_NAME = "v.sock"       # under {session_dir}/ipc/
API_SOCK_NAME = "firecracker.sock"
WORKSPACE_IMG_NAME = "workspace.img"
LOG_NAME = "firecracker.log"
METRICS_NAME = "metrics.json"

# vsock CIDs: 0, 1 (reserved), 2 (host). Guests get 3 .. 0xFFFFFFFE; we stay
# well under that so a CID is always a plain positive int in JSON.
MIN_GUEST_CID = 3
_CID_SPAN = 2**31 - MIN_GUEST_CID

_INSTANCE_ID_RE = re.compile(r"[^A-Za-z0-9-]")

# sun_path is 108 bytes on Linux. A session directory deep under data/sessions
# can push `{session_dir}/ipc/v.sock_5010` past it, so socket paths handed to
# Firecracker are made relative to its cwd ({session_dir}/vm) when the absolute
# form is too long, and the host side connects through /proc/self/fd/<dirfd>.
UNIX_PATH_MAX = 100   # leave room for Firecracker's own "_PORT" suffixes


class SandboxError(RuntimeError):
    """Launch, API or lifecycle failure. API failures carry Firecracker's
    response body verbatim (`status`, `body`)."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def derive_guest_cid(name: str) -> int:
    """Per-session guest CID chosen by the host: >= 3, stable for a session name.

    Firecracker's vhost-vsock backend needs CIDs unique per host at the same
    time; 2^31 values hashed from the session name make a clash between two
    concurrent sessions negligible, and the worker may override via
    `FirecrackerConfig.guest_cid` when it allocates CIDs itself."""
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
        )
        if e.get("HARNESS_GUEST_CID"):
            cfg.guest_cid = int(e["HARNESS_GUEST_CID"])
        return cfg

    def validate(self) -> None:
        if not self.kernel:
            raise SandboxError("FirecrackerConfig.kernel is empty (HARNESS_KERNEL)")
        if not self.rootfs:
            raise SandboxError("FirecrackerConfig.rootfs is empty (HARNESS_ROOTFS)")
        if self.vcpus < 1 or self.mem_mib < 128 or self.workspace_mib < 1:
            raise SandboxError("vcpus >= 1, mem_mib >= 128, workspace_mib >= 1 required")
        if self.guest_cid is not None and self.guest_cid < MIN_GUEST_CID:
            raise SandboxError(f"guest_cid must be >= {MIN_GUEST_CID}")


def _fits_sun_path(path: str) -> bool:
    return len(path.encode("utf-8")) <= UNIX_PATH_MAX


def firecracker_path(path: Path, cwd: Path) -> str:
    """The form of a socket path Firecracker receives: absolute when it fits
    sun_path, else relative to the Firecracker process's cwd."""
    absolute = str(path)
    if _fits_sun_path(absolute):
        return absolute
    return os.path.relpath(absolute, str(cwd))


def connect_unix(path: Path, timeout: float | None) -> socket.socket:
    """connect() to a Unix socket at any path length (via /proc/self/fd when
    the absolute path would not fit sun_path)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        absolute = str(path)
        if _fits_sun_path(absolute):
            sock.connect(absolute)
        else:
            dirfd = os.open(str(path.parent), os.O_PATH | os.O_DIRECTORY)
            try:
                sock.connect(f"/proc/self/fd/{dirfd}/{path.name}")
            finally:
                os.close(dirfd)
    except BaseException:
        sock.close()
        raise
    return sock


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a Unix socket (the Firecracker API)."""

    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = connect_unix(self._path, self.timeout)


class FirecrackerSandbox(_SandboxBase):
    """One microVM. `start(session_dir)`, `stop()`, `is_alive()` are the
    Sandbox interface; the rest is for the worker and the tests. A stopped
    sandbox may be started again (a fresh VM on the same workspace.img).
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

    # ------------------------------------------------------------------ API

    def start(self, session_dir) -> None:
        if self.is_alive():
            raise SandboxError("sandbox already started")
        self._proc = None
        self.config.validate()
        self.session_dir = Path(session_dir)
        self.vm_dir = self.session_dir / "vm"
        ipc_dir = self.session_dir / "ipc"
        self.vm_dir.mkdir(parents=True, exist_ok=True)
        ipc_dir.mkdir(parents=True, exist_ok=True)

        self.api_socket = self.vm_dir / API_SOCK_NAME
        self.vsock_uds = ipc_dir / VSOCK_NAME
        self.workspace_image = self.vm_dir / WORKSPACE_IMG_NAME
        self.log_path = Path(self.config.log_path) if self.config.log_path else self.vm_dir / LOG_NAME
        self.metrics_path = (
            Path(self.config.metrics_path) if self.config.metrics_path else self.vm_dir / METRICS_NAME
        )
        self.instance_id = instance_id(self.session_dir.name)
        self.guest_cid = (
            self.config.guest_cid
            if self.config.guest_cid is not None
            else derive_guest_cid(self.session_dir.name)
        )

        self._ensure_workspace_image()
        # Firecracker binds both sockets itself; stale files make it fail.
        _unlink(self.api_socket)
        _unlink(self.vsock_uds)
        self.log_path.touch(exist_ok=True)
        self.metrics_path.touch(exist_ok=True)

        self._launch()
        try:
            self._wait_for_api()
            self._configure()
        except BaseException:
            self._kill()
            raise

    def stop(self) -> None:
        """Graceful: SendCtrlAltDel (the guest init syncs /workspace and powers
        off, Firecracker exits); SIGKILL after `stop_grace_s`."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self._request("PUT", "/actions", {"action_type": "SendCtrlAltDel"})
            except SandboxError:
                pass  # already gone or API dead: fall through to the kill
            try:
                proc.wait(timeout=self.config.stop_grace_s)
            except subprocess.TimeoutExpired:
                self._kill()
        self._last_returncode = proc.returncode
        self._proc = None
        self._cleanup_sockets()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

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
        return self.vsock_uds.with_name(f"{self.vsock_uds.name}_{port}")

    # ------------------------------------------------------------ internals

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
        console = open(self.vm_dir / "console.log", "ab")
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=console,         # the guest's ttyS0 (boot messages, init output)
                stderr=subprocess.STDOUT,
                cwd=str(self.vm_dir),
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
                    f"(see {self.vm_dir / 'console.log'})"
                )
            if self.api_socket.exists():
                try:
                    connect_unix(self.api_socket, 0.5).close()
                    return
                except OSError:
                    pass
            if time.monotonic() > deadline:
                raise SandboxError(f"firecracker API socket {self.api_socket} not ready")
            time.sleep(0.02)

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
                "path_on_host": str(Path(cfg.rootfs)),
                "is_root_device": True,
                "is_read_only": True,
            },
            "/drives/workspace": {
                "drive_id": "workspace",
                "path_on_host": str(self.workspace_image),
                "is_root_device": False,
                "is_read_only": False,
            },
            "/vsock": {
                "guest_cid": self.guest_cid,
                "uds_path": firecracker_path(self.vsock_uds, self.vm_dir),
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

    def _cleanup_sockets(self) -> None:
        _unlink(self.api_socket)
        _unlink(self.vsock_uds)


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
