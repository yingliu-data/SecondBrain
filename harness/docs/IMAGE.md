# Guest VM image (`harness/image/`)

What boots inside the per-session Firecracker microVM (PLAN.md D6a, D18, M1,
M3), how it is built, how the base snapshot is taken, and how the guest finds
the host. Build and runtime are both unprivileged; nothing here needs sudo.
The server runbooks are `docs/RUNBOOK-M1.md` (first boot) and
`docs/RUNBOOK-M3.md` (snapshots, park and resume).

## Artifacts (`harness/image/out/`, git-ignored)

| File | What | Built by |
|---|---|---|
| `guest.pyz` | `harness/guest` + `harness/protocol` as one stdlib zipapp with precompiled bytecode (see "guest.pyz bytecode"); `python3 guest.pyz` runs `guest.main.main()` | `build.sh pyz` (`pyz_pack.py`) |
| `rootfs.ext4` | the read-only root filesystem (Debian bookworm-slim, Python 3.12, bash, git, coreutils, `/sbin/harness-init`, `/opt/harness/guest.pyz`, `/opt/harness/IMAGE_VERSION`); ~350 MiB | `build.sh rootfs` |
| `GUEST_IMAGE_VERSION` | 16 hex chars: sha256 over the sha256 of `guest.pyz`, `Dockerfile.rootfs`, `harness-init`, `pyz_pack.py` and `versions.env`, `[:16]`; baked into the rootfs as `/opt/harness/IMAGE_VERSION` (see "Image identities") | `build.sh rootfs` |
| `vmlinux` | the pinned Firecracker CI kernel, uncompressed ELF, sha256-verified | `build.sh kernel` |
| `workspace-template.img` | a 1 GiB sparse, empty ext4; `HARNESS_WORKSPACE_TEMPLATE`, the fallback when there is no `mkfs.ext4` (the agent-api image has it) | `build.sh workspace` |
| `IMAGE_VERSION` | 16 hex chars: `sha256("<vmlinux sha256>\n<rootfs sha256>\n")[:16]`; the identity every snapshot records and is checked against (see "Image identities") | `build.sh version` (part of `all`) |
| `firecracker`, `jailer` | the pinned release binaries for host-side runs (`make snapshot`); the agent-api image installs its own copy from the same pin | `build.sh firecracker` |
| `vm/base/{vmstate,mem,MANIFEST.json}` | the BASE snapshot, when built with the default `HARNESS_VM_CACHE=out/vm` (normally built straight into the deployed cache) | `make snapshot` (needs `/dev/kvm`) |

### Image identities

Two, because one cannot hash a file that contains its own hash:

- `out/IMAGE_VERSION` (kernel + rootfs bytes) is the **snapshot identity**:
  `MANIFEST.json`/`META.json` record it, `restore()` refuses a snapshot taken
  from other bytes. It sits next to the rootfs (`HARNESS_IMAGE_VERSION`
  overrides it).
- `/opt/harness/IMAGE_VERSION` inside the rootfs (`out/GUEST_IMAGE_VERSION`,
  the rootfs's inputs) is the **guest's own identity**: `harness-init` passes
  it as `--image-version`, so `session_start.image_version` names the image a
  run used (`unknown` when the file is missing, `dev` for a guest started
  without the flag: tests, LocalSandbox).

The harness release itself is `protocol.version.HARNESS_VERSION`: the host
sends it in `hello.config.harness_version`, the guest records its own in
`session_start.harness_version`, and the host logs a `harness_skew` host event
(a warning, never fatal) when they differ.

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
- `/opt/harness/guest.pyz` (mode 0755, its `.pyc` recompiled by the image's
  own python3), `/opt/harness/IMAGE_VERSION` and `/sbin/harness-init` (mode 0755).
- Bytecode for the standard library (`compileall --invalidation-mode
  unchecked-hash`, without test/idlelib/tkinter/ensurepip/lib2to3/pydoc_data;
  about +17 MiB): the official python image ships no `.pyc`, and on the
  read-only rootfs with `PYTHONDONTWRITEBYTECODE=1` every import would
  otherwise compile from source.
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
3. Does **not** mount `/dev/vdb` (GUEST.md "Snapshot point"): the guest mounts it on
   `/workspace` itself once idle-wait ends (`--mount-workspace /dev/vdb`), so
   the base snapshot never holds a mounted workspace. Without a `/dev/vdb`
   (a hand-run boot) init puts a tmpfs on `/workspace` and drops the flag.
4. **Clock:** nothing. The VM has no RTC source worth trusting and no NTP; the
   guest takes the time from `hello.at` and every later `heartbeat`
   (PLAN.md section 4.3) and the protocol never folds wall-clock time.
5. Runs the guest as a child:
   `python3 /opt/harness/guest.pyz --connect vsock --idle-wait vsock --mount-workspace /dev/vdb --log-dir /var/harness --workspace /workspace --image-version "$(cat /opt/harness/IMAGE_VERSION)"`
   and waits. The guest sits in idle-wait (a silent connect to CID 2:5000
   every 5 ms) until a host listener answers; that is the base-snapshot
   point. Before it starts polling, the guest has imported every module a
   session needs (`guest.main.preload()`), so they are in the snapshot's
   memory. A cold boot takes exactly the same path. After `parked` the guest
   returns to idle-wait without exiting (PROTOCOL.md section 5), so init does not
   notice a park.
6. On guest exit, or on SIGINT/SIGTERM (the host's `stop()`): TERM then KILL the
   guest, `sync`, unmount `/workspace`, and `reboot(LINUX_REBOOT_CMD_RESTART)`
   (sysrq `b` as fallback). With `reboot=k` Firecracker sees the reset and the
   Firecracker process exits, which is how the host observes the VM's end.

Console output (kernel + init + the guest's stderr) goes to ttyS0, which the
sandbox manager captures at `{session_dir}/vm/console.log`.

## The base snapshot (`make snapshot`, M3)

`build_snapshot.py build-base` (the logic is `build_base_snapshot()` in
`agent-api/app/host/sandbox/base_snapshot.py`; the script puts `agent-api/`
and `harness/` on `sys.path` and runs its `main()` with a bare `python3`, the
container runs `python -m app.host.sandbox.base_snapshot build-base`;
`python -m app.host.sandbox.firecracker build-base` still works):

1. Makes a scratch session dir `{cache}/.build-base/{vm,ipc}` and cold-boots the
   rootfs exactly as `FirecrackerSandbox.start()` does, with guest CID 3
   (`BASE_GUEST_CID`). Every path the VM records is relative to the
   Firecracker cwd (`vm/`): drives `rootfs.ext4` (a symlink to `HARNESS_ROOTFS`)
   and `workspace.img`, vsock `../ipc/v.sock`.
2. Waits for idle: no host listener exists, so each connect attempt is a vsock
   packet the VMM answers with a reset. `FlushMetrics` every 200 ms; three
   consecutive windows with vsock packets = the guest is in its idle loop
   (nothing else in the guest uses vsock before hello); then 1 s settle.
   `--fixed-delay S` replaces detection with a plain wait.
3. `PATCH /vm {Paused}`, `PUT /snapshot/create {Full, snapshot_path, mem_file_path}`
   into `{cache}/.base.tmp/`, writes `MANIFEST.json`, kills the process and
   swaps `.base.tmp` -> `base` atomically (running VMs keep the old `mem` inode).

`MANIFEST.json`: `format`, `image_version`, `kernel_sha256`, `rootfs_sha256`,
`firecracker_version`, `guest_cid`, `vcpus`, `mem_mib`, `workspace_bytes`,
`boot_args`, `idle_detect`, `created_ms`. `restore()` refuses (SandboxError ->
the worker boots the rootfs instead) when `image_version` or
`firecracker_version` differ from the running config, or when the session's
`workspace.img` is not `workspace_bytes` long (the virtio-blk capacity is part
of the vmstate). Rebuild the base after every `make image`, Firecracker
upgrade, or change of `HARNESS_VM_MEM_MIB` / `HARNESS_VM_VCPUS` /
`HARNESS_WORKSPACE_MIB`.

Why relative paths work (Firecracker v1.17, `docs/snapshotting/snapshot-support.md`):
the vmstate stores the drive `path_on_host` and vsock `uds_path` strings as
given, and a new process re-opens them; resources "need to be accessible at
the same relative paths to the new Firecracker process". The sandbox launches
every Firecracker with `cwd = {session_dir}/vm`, so `workspace.img` and
`../ipc/v.sock` resolve to each session's own files (this is also exactly what
the jailer's chroot layout expects). `/snapshot/load` additionally gets
`vsock_override {uds_path}` (added in Firecracker 1.16); there is no drive
override in the 1.17 API. The guest CID is restored from the vmstate and
cannot be overridden: all VMs from one base share CID 3, which is harmless
because Firecracker's vsock is emulated per VMM over Unix sockets, not
vhost-vsock. Firecracker treats resuming one snapshot many times as a
randomness hazard (VMGenID reseeds the kernel PRNG on 6.1); the guest draws
nothing random before the snapshot point and derives every id from hello
(GUEST.md "Snapshot point").

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

The guest's own CID is irrelevant to it (it only dials CID 2). Firecracker's
vsock is emulated inside each VMM and bridged to that VM's own Unix sockets,
so CIDs need not be unique across VMs: a cold boot uses
`derive_guest_cid(session name)` (overridable with `HARNESS_GUEST_CID`), and
every VM restored from the base snapshot carries the base's CID 3.

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

### guest.pyz bytecode

`pyz_pack.py pack STAGE OUT` (called by `build.sh pyz` instead of `python -m
zipapp`) writes every `x.py` plus a legacy-layout `x.pyc` next to it (zipimport
never reads `__pycache__/`), compiled by the running interpreter as an
unchecked-hash pyc (PEP 552: no mtime inside, no source check at import; the
rootfs is read-only). Entries are sorted with fixed 1980-01-01 timestamps and
modes, so the same sources and interpreter give the same bytes. Each code
object's filename is `/opt/harness/guest.pyz/<module path>.py`, what zipimport
would set compiling from source. `Dockerfile.rootfs` runs `pyz_pack.py
recompile` with the guest's own python3 (a pyc from a different 3.x has another
magic number and zipimport would silently fall back to the source) and fails
the build unless `guest.main` then loads from `.pyc`.

`measure_imports.py PYZ [--runs N] [--python CMD]` times, in fresh
interpreters, what the guest imports before the idle-wait (`import guest.main`
+ `preload()`, i.e. what the snapshot holds) and what a session imports after
hello. Measured on the dev box (x86_64, 9 runs, medians, `python3 -I -B`
inside the rootfs docker image, i.e. the guest's Python 3.12.15):

| | before idle-wait | after hello (every restore) |
|---|---|---|
| before (zipapp, no `.pyc`, lazy session imports, no stdlib `.pyc`) | 339 ms | 556 ms (103 modules) |
| after (bytecode pyz + stdlib `.pyc` + `preload()`) | 142 ms | 0.06 ms (0 modules) |

Run it against the image you built:
`docker run --rm -v $PWD:/img:ro secondbrain/harness-rootfs:local python3 /img/measure_imports.py /opt/harness/guest.pyz --python "python3 -I -B"`.

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
`versions.env` (sha256 from the release's `.sha256.txt`). `build.sh
firecracker` and the agent-api image (`agent-api/Dockerfile`, a download
stage that sources `versions.env` and checks the sha256) both read it:
rebuild the image, then the base snapshot.

At runtime docker-compose mounts `out/` read-only at `/opt/harness-image`
in agent-api (`HARNESS_KERNEL`, `HARNESS_ROOTFS`,
`HARNESS_WORKSPACE_TEMPLATE` point there) and the VM cache at
`/var/lib/secondbrain/vm`; see `RUNBOOK-M3.md` §0.

## Limits and follow-ups

- Snapshots are Full only (no Diff snapshots, no UFFD memory backend). A
  2 GiB guest writes up to 2 GiB of `mem` per park (sparse: only touched
  pages occupy disk); Diff snapshots are the follow-up if parks get frequent.
- The init script still reboots the VM when the guest exits; a park does not
  exit the guest (it returns to idle-wait), so this only covers crashes and
  `stop()`.
- The jailer is not used in M1 (PLAN.md section 11: isolation hardening is
  OpenShell's); `out/jailer` is extracted so the switch is a flag change in
  `sandbox/firecracker.py`.
- A project toolchain for the default tenant (node, Python packages for the
  *workspace*, not the guest) is PLAN.md open question 4; it would be a second
  Dockerfile stage, never a change to the guest's stdlib rule.
