# Guest VM image (`harness/image/`)

What boots inside the per-session Firecracker microVM (PLAN.md D6a, D18, M1),
how it is built, and how the guest finds the host. Build and runtime are both
unprivileged; nothing here needs sudo. The server runbook is
`docs/RUNBOOK-M1.md`.

## Artifacts (`harness/image/out/`, git-ignored)

| File | What | Built by |
|---|---|---|
| `guest.pyz` | `harness/guest` + `harness/protocol` as one stdlib zipapp; `python3 guest.pyz` runs `guest.main.main()` | `build.sh pyz` |
| `rootfs.ext4` | the read-only root filesystem (Debian bookworm-slim, Python 3.12, bash, git, coreutils, `/sbin/harness-init`, `/opt/harness/guest.pyz`); ~350 MiB | `build.sh rootfs` |
| `vmlinux` | the pinned Firecracker CI kernel, uncompressed ELF, sha256-verified | `build.sh kernel` |
| `workspace-template.img` | a 1 GiB sparse, empty ext4; `HARNESS_WORKSPACE_TEMPLATE` for hosts whose agent-api container has no `mkfs.ext4` | `build.sh workspace` |
| `firecracker`, `jailer` | the pinned release binaries (host side, optional convenience) | `build.sh firecracker` |

Pins live in `harness/image/versions.env` (Firecracker `v1.17.0`, CI kernel
`firecracker-ci/v1.15/x86_64/vmlinux-6.1.155`, base image
`python:3.12-slim-bookworm`) with the URL and sha256 of each download.

## What the rootfs contains

`Dockerfile.rootfs`, from `python:3.12-slim-bookworm`:

- Debian packages: `bash coreutils util-linux mount procps git grep findutils
  sed gawk ca-certificates less file xz-utils zip unzip`. These back the guest
  built-in tools (`bash`, `read`, `write`, `edit`, `grep`, `glob`, `ls`).
- `/usr/bin/python3` -> the image's Python 3.12. **No Python packages**: the guest
  is standard library only (PLAN.md ground rule 1, enforced by
  `harness/tests/test_purity.py`). There is nothing to pip-install and no pip
  step; "updating Python packages" means bumping `ROOTFS_BASE_IMAGE` in
  `versions.env` for a newer 3.12.x and rebuilding.
- `/opt/harness/guest.pyz` (mode 0755) and `/sbin/harness-init` (mode 0755).
- `/workspace` (mount point for `/dev/vdb`), `/var/harness` (tmpfs at boot, the
  guest's event-log buffer), `/etc/hostname` = `harness`.
- git configured system-wide: `user.name harness`, `safe.directory *`,
  `init.defaultBranch main`.
- No network configuration, no DNS (`/etc/resolv.conf` is empty), no sshd, no
  users besides root. The VM has no network device at all (`PUT /network-interfaces`
  is never called), so nothing in the image can reach anything but vsock.

Apt lists, caches, `/usr/share/doc` and `/usr/share/man` are removed. Every
file is owned by root (the ext4 is populated inside a root build container).

## The init script (`/sbin/harness-init`)

PID 1 is a bash script (source: `harness/image/harness-init`). Boot args from
the sandbox manager: `console=ttyS0 reboot=k panic=1 pci=off init=/sbin/harness-init`;
Firecracker appends `root=/dev/vda ro` for the drive marked `is_root_device`.

1. Mounts `/proc`, `/sys`, `/dev` (devtmpfs, usually already there), `/dev/pts`,
   `/dev/shm`, and tmpfs on `/run`, `/tmp`, `/var/harness`, `/root`. The root
   filesystem stays read-only.
2. Sets the hostname and `kernel/ctrl-alt-del = 0` (soft: the host's
   `SendCtrlAltDel` becomes SIGINT to PID 1 instead of an instant reboot).
3. Mounts `/dev/vdb` (the session's `workspace.img`) at `/workspace`,
   `noatime`. Without a `/dev/vdb` it falls back to a tmpfs and logs it.
4. **Clock:** nothing. The VM has no RTC source worth trusting and no NTP; the
   guest takes the time from `hello.at` and every later `heartbeat`
   (PLAN.md section 4.3) and the protocol never folds wall-clock time.
5. Runs the guest as a child:
   `python3 /opt/harness/guest.pyz --connect vsock --log-dir /var/harness --workspace /workspace`
   and waits.
6. On guest exit, or on SIGINT/SIGTERM (the host's `stop()`): TERM then KILL the
   guest, `sync`, unmount `/workspace`, and `reboot(LINUX_REBOOT_CMD_RESTART)`
   (sysrq `b` as fallback). With `reboot=k` Firecracker sees the reset and the
   Firecracker process exits, which is how the host observes the VM's end.

Console output (kernel + init + the guest's stderr) goes to ttyS0, which the
sandbox manager captures at `{session_dir}/vm/console.log`.

## How the guest finds the host

The guest never listens; it connects out over `AF_VSOCK` to **CID 2** (the
host) on the fixed ports (`docs/PROTOCOL.md`):

| Port | Channel |
|---|---|
| 5000 | control: `hello`, control messages, events, acks (JSONL) |
| 5001 | bulk: spills, workspace manifest |
| 5010 | LLM: HTTP/1.1 to the LLM proxy |
| 5011 | gateway (M4) |

Firecracker's vsock device is configured by the sandbox manager with
`guest_cid` = a per-session value >= 3 chosen by the host and
`uds_path = {session_dir}/ipc/v.sock`. A guest `connect(2, P)` is forwarded
to the host Unix socket **`{session_dir}/ipc/v.sock_P`** - the same names the
test double uses (`guest.main --connect unix:{session_dir}/ipc`), so
`app/host/endpoints.py` serves a VM and a local guest with one listener.
Host-initiated connections (none in M1) would go through `v.sock` itself with
Firecracker's `CONNECT P\n` / `OK n\n` handshake.

The guest's own CID is irrelevant to it (it only dials CID 2); the host needs
it unique per running VM, which `derive_guest_cid(session name)` in
`sandbox/firecracker.py` gives, overridable with `HARNESS_GUEST_CID`.

## Building and rebuilding

```sh
cd harness/image
make image        # pyz -> rootfs.ext4 -> vmlinux -> workspace-template.img
make check        # sizes, kernel sha256, `python3 out/guest.pyz --help`, rootfs contents
make firecracker  # host binaries into out/ (or install them system-wide, see the runbook)
make clean
```

Each step is idempotent: `guest.pyz` is rebuilt when `harness/guest` or
`harness/protocol` changed; `rootfs.ext4` when the pyz, `Dockerfile.rootfs`,
`harness-init` or `versions.env` changed; downloads are skipped when the
sha256 already matches. Needs: python3 >= 3.12, docker, curl, sha256sum, make.

The rootfs path is `docker build` -> `docker create`/`docker export` (a tar of
the container filesystem) -> `mkfs.ext4 -F -d <tree> -E root_owner=0:0`
(e2fsprogs' populate-from-directory, no loop mount, no root). By default
(`MKFS_MODE=auto`) the extraction and mkfs run inside a throwaway
`python:3.12-slim-bookworm` container with e2fsprogs so files keep root
ownership; `MKFS_MODE=host` uses the host's `mkfs.ext4` instead (with
`fakeroot` when present; without it the files are owned by the build user,
which the root-only guest does not mind). Hosts whose e2fsprogs is older than
1.43 have no `-d`: use `MKFS_MODE=docker` there.

Image size is measured: tree size + 25 % + 64 MiB (`ROOTFS_MIB` in
`versions.env` pins it instead).

### Changing the kernel

List what the bucket has, pick, pin:

```sh
S3=https://s3.amazonaws.com/spec.ccfc.min
curl -fsSL "$S3?list-type=2&prefix=firecracker-ci/v1.15/x86_64/vmlinux-" | grep -oP '(?<=<Key>)[^<]+'
curl -fsSLO "$S3/firecracker-ci/v1.15/x86_64/vmlinux-6.1.155" && sha256sum vmlinux-6.1.155
```

Put `KERNEL_URL` and `KERNEL_SHA256` into `versions.env`; `make kernel`. The
`.config` next to each kernel shows what is built in; the harness needs
`VIRTIO_VSOCKETS`, `VIRTIO_BLK`, `EXT4_FS`, `TMPFS`, `DEVTMPFS` (all `=y` in
the pinned one). Tag-named CI directories (`v1.15`) are stable; the
date-named ones (`firecracker-ci/YYYYMMDD-<sha>-0/`) roll and are not pinned.

### Changing Firecracker

`FIRECRACKER_VERSION`, `FIRECRACKER_URL`, `FIRECRACKER_SHA256` in
`versions.env` (sha256 from the release's `.sha256.txt`); the runbook's
install step reads the same file.

## Limits and follow-ups

- No snapshot in the image yet: M3 adds the "harness idle before it connects
  out" snapshot (the guest already has `--idle-wait FILE` for that point) and
  per-session snapshots.
- The init script reboots the VM when the guest exits. In M3, where a VM
  spans a session and is parked rather than ended, the init will instead
  restart the guest or wait for a `park` acknowledgement.
- The jailer is not used in M1 (PLAN.md section 11: isolation hardening is
  OpenShell's); `out/jailer` is extracted so the switch is a flag change in
  `sandbox/firecracker.py`.
- A project toolchain for the default tenant (node, Python packages for the
  *workspace*, not the guest) is PLAN.md open question 4; it would be a second
  Dockerfile stage, never a change to the guest's stdlib rule.
