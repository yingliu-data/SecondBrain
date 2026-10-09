# M1 runbook: the harness in a Firecracker microVM on the server

For the owner's Linux box (RTX 5080, `/dev/kvm`). The build machine has no
KVM and no sudo, so everything below was prepared there and runs for the
first time here. Read `docs/IMAGE.md` for what boots and `docs/GUEST.md` /
`docs/PROTOCOL.md` for what the guest does once it is up.

Status of the pieces this runbook drives:

| Piece | State on the build machine |
|---|---|
| `agent-api/app/host/sandbox/firecracker.py` | unit-tested against a fake Firecracker (`tests/test_sandbox_firecracker.py`, 22 tests): API order and bodies, read-only rootfs, vsock naming, stop, SIGKILL, 400 -> `SandboxError` |
| `harness/image/` | `make image` and `make check` ran end to end: `guest.pyz`, `rootfs.ext4` (346 MiB), `vmlinux` (6.1.155, sha256 ok), `workspace-template.img` |
| a real boot | **not possible without KVM** - step 4 below is the first one |

## 1. Install Firecracker (pinned)

Versions come from `harness/image/versions.env` (`v1.17.0` at the time of writing).

```sh
cd harness/image && source versions.env
curl -fL -o /tmp/fc.tgz "$FIRECRACKER_URL"
echo "$FIRECRACKER_SHA256  /tmp/fc.tgz" | sha256sum -c
tar -xzf /tmp/fc.tgz -C /tmp
sudo install -m 0755 /tmp/release-$FIRECRACKER_VERSION-x86_64/firecracker-$FIRECRACKER_VERSION-x86_64 /usr/local/bin/firecracker
sudo install -m 0755 /tmp/release-$FIRECRACKER_VERSION-x86_64/jailer-$FIRECRACKER_VERSION-x86_64 /usr/local/bin/jailer
firecracker --version
```

(`make firecracker` does the download/verify/extract into `out/` without sudo
if you prefer to bind-mount the binary into the container.)

## 2. KVM and vsock access for the agent-api user

```sh
ls -l /dev/kvm /dev/vhost-vsock          # crw-rw---- root kvm
sudo modprobe vhost_vsock                # if /dev/vhost-vsock is missing
echo vhost_vsock | sudo tee /etc/modules-load.d/vhost_vsock.conf
sudo usermod -aG kvm "$USER"             # the user that runs docker compose / agent-api
newgrp kvm                                # or log out and in
[ -r /dev/kvm ] && [ -w /dev/kvm ] && echo kvm ok
```

If `/dev/vhost-vsock` is owned by `root:root` with mode 600 on this distro,
add a udev rule so the kvm group can open it:

```sh
echo 'KERNEL=="vhost-vsock", GROUP="kvm", MODE="0660"' | sudo tee /etc/udev/rules.d/99-vhost-vsock.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Inside the container the process runs as root, so only the device nodes
need to be passed through (next step); the group membership matters for
running `firecracker` by hand as yourself (step 4).

## 3. Build the image on the server

```sh
cd harness/image
make image && make check
```

Needs docker (the compose host already has it), python3 >= 3.12, curl, make.
Outputs land in `harness/image/out/`. Copy them to the host-side cache the
container mounts (any path; the snippet below uses `~/data/SecondBrain/vm`):

```sh
mkdir -p ~/data/SecondBrain/vm
cp out/vmlinux out/rootfs.ext4 out/workspace-template.img ~/data/SecondBrain/vm/
```

## 4. Boot one VM by hand (first ever boot: do this before touching compose)

This checks the kernel, the rootfs, the init script and the vsock device with
nothing else involved. It mirrors exactly what `FirecrackerSandbox.start()`
sends, so if this works the sandbox manager works.

```sh
S=/tmp/fc-smoke; rm -rf $S; mkdir -p $S/vm $S/ipc
IMG=~/data/SecondBrain/vm
truncate -s 256M $S/vm/workspace.img && mkfs.ext4 -F -q $S/vm/workspace.img

# a stand-in for the host's :5000 listener, so the guest's connect() lands somewhere
( socat -d -d -v UNIX-LISTEN:$S/ipc/v.sock_5000,fork - 2>&1 | tee $S/control.log ) &

firecracker --api-sock $S/vm/firecracker.sock --id smoke \
            --log-path $S/vm/firecracker.log --level Info &
sleep 0.3
api() { curl -sS --unix-socket $S/vm/firecracker.sock -X PUT "http://localhost$1" \
             -H 'Content-Type: application/json' -d "$2"; echo; }
api /machine-config   '{"vcpu_count": 2, "mem_size_mib": 2048, "smt": false}'
api /boot-source      "{\"kernel_image_path\": \"$IMG/vmlinux\", \"boot_args\": \"console=ttyS0 reboot=k panic=1 pci=off init=/sbin/harness-init\"}"
api /drives/rootfs    "{\"drive_id\": \"rootfs\", \"path_on_host\": \"$IMG/rootfs.ext4\", \"is_root_device\": true, \"is_read_only\": true}"
api /drives/workspace "{\"drive_id\": \"workspace\", \"path_on_host\": \"$S/vm/workspace.img\", \"is_root_device\": false, \"is_read_only\": false}"
api /vsock            "{\"guest_cid\": 3, \"uds_path\": \"$S/ipc/v.sock\"}"
api /actions          '{"action_type": "InstanceStart"}'
```

Expected on the terminal (ttyS0): the kernel log, then
`harness-init: starting guest harness (idle-wait, then vsock -> CID 2)`.
The guest starts in `--idle-wait vsock` (since M3: the base-snapshot point):
it polls :5000 every 50 ms, silently, and once connected sends nothing until
the host sends its first line. So `$S/control.log` shows socat's
`accepting connection` line and no data; without `/dev/vdb` mounted yet
(the guest mounts it with `--mount-workspace /dev/vdb` only after
idle-wait) that is the whole boot check. Typing any line into socat ends
idle-wait (`guest.main INFO idle-wait over: the host answered on :5000` on
the console); with no `hello` after it the guest exits after 30 s
(`no hello within 30s`, exit 3) and init reboots the VM. Stop it:

```sh
api /actions '{"action_type": "SendCtrlAltDel"}'    # init: sync, umount, reboot -> firecracker exits
```

If a step fails, the message in the API reply is what `SandboxError` would
carry. Common ones: `Cannot open /dev/kvm` (step 2), `vsock: ... vhost-vsock`
(module not loaded), `Cannot bind uds_path` (a stale `v.sock`; the sandbox
manager removes it before `PUT /vsock`).

## 5. Verify the no-network property from inside the VM

The VM has no network device: `PUT /network-interfaces` is never called and
the rootfs carries no network configuration. Prove it from inside, once,
with a throwaway boot args line that drops you into a shell instead of the
harness (`init=/bin/bash` and a writable serial console):

```sh
api /boot-source "{\"kernel_image_path\": \"$IMG/vmlinux\", \"boot_args\": \"console=ttyS0 reboot=k panic=1 pci=off init=/bin/bash\"}"
# ... same drives/vsock as in step 4, then InstanceStart, then in the guest shell:
mount -t proc proc /proc; mount -t sysfs sys /sys
ls /sys/class/net                      # expect: lo   (and nothing else)
cat /proc/net/dev                      # only lo
python3 - <<'EOF'
import socket
for host in ("1.1.1.1", "10.0.0.1"):
    try:
        socket.create_connection((host, 53), timeout=2); print("REACHED", host)
    except OSError as e:
        print("blocked", host, e)
try:
    socket.getaddrinfo("example.com", 80); print("DNS RESOLVED (unexpected)")
except OSError as e:
    print("no DNS:", e)
s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM); print("vsock socket ok:", s)
EOF
ls /dev/vsock /dev/vda /dev/vdb
```

Expected: `lo` only, every connect `Network is unreachable`, DNS fails, and
`/dev/vsock` exists. Record the output in the M1 exit notes.

## 6. Run one session through agent-api

### 6a. The host's local test path (no VM) - works on any machine

`HARNESS_SANDBOX=local` (the default) runs the guest as a subprocess
(`python -m guest.main --connect unix:{session_dir}/ipc --log-dir
{session_dir}/guest --workspace {session_dir}/vm/workspace`) against the same
`{session_dir}/ipc/v.sock_{port}` listeners. `{session_dir}/vm/workspace` is
the guest's private workspace (the analogue of `vm/workspace.img`); the
host's `{session_dir}/workspace` is written only by the bulk server. Use it
to separate "the loop" from "the VM". Every tenant runs the harness engine
(there is no `AGENT_ENGINE` any more), and `/api/v1/chat` wants Bearer +
`X-Timestamp` + `X-Signature` (HMAC-SHA256 of timestamp + body with the
key, `app/auth/middleware.py`):

```sh
cd agent-api
HARNESS_SANDBOX=local API_SECRET_KEY=... LLM_URL=http://localhost:8080 \
  uv run uvicorn app.main:app --port 8000
chat() { ts=$(date +%s)
  sig=$(printf '%s%s' "$ts" "$1" | openssl dgst -sha256 -hmac "$API_SECRET_KEY" -r | cut -d' ' -f1)
  curl -sN -X POST localhost:8000/api/v1/chat -H "Authorization: Bearer $API_SECRET_KEY" \
       -H "X-Timestamp: $ts" -H "X-Signature: $sig" -H 'Content-Type: application/json' -d "$1"; }
chat '{"session_id": "smoke1", "message": "What is in my workspace?", "input_id": "i-1"}'
```

### 6b. The same session in the VM

```sh
HARNESS_SANDBOX=firecracker \
HARNESS_FIRECRACKER_BIN=/usr/local/bin/firecracker \
HARNESS_KERNEL=$HOME/data/SecondBrain/vm/vmlinux \
HARNESS_ROOTFS=$HOME/data/SecondBrain/vm/rootfs.ext4 \
HARNESS_WORKSPACE_TEMPLATE=$HOME/data/SecondBrain/vm/workspace-template.img \
API_SECRET_KEY=... LLM_URL=http://localhost:8080 uv run uvicorn app.main:app --port 8000
```

Then the same `chat`. What to look at per session directory
`data/sessions/{user}/{tenant}_{session}/`:

| Path | Content |
|---|---|
| `vm/console.log` | kernel + init + guest stderr (ttyS0) |
| `vm/firecracker.log`, `vm/metrics.json` | Firecracker's own log and metrics |
| `vm/workspace.img` | the session's `/workspace` (`debugfs -R ls` to peek without mounting) |
| `ipc/v.sock`, `ipc/v.sock_5000`, `_5001`, `_5010`, `_5011` | the vsock uds and the host listeners (control, bulk, LLM proxy, MCP gateway forwarder) |
| `events/r0001/000001.jsonl` | the canonical log (state sync), one `rNNNN/` per fresh guest |
| `events/host-000001.jsonl` | the host log (`vm`, `workflow`, `usage`, `side_call`, ... records) |

Every `HARNESS_*` variable the sandbox reads: `HARNESS_FIRECRACKER_BIN`,
`HARNESS_KERNEL`, `HARNESS_ROOTFS`, `HARNESS_VM_VCPUS` (2), `HARNESS_VM_MEM_MIB`
(2048), `HARNESS_WORKSPACE_MIB` (1024, when `mkfs.ext4` creates the image),
`HARNESS_WORKSPACE_TEMPLATE` (copied sparsely when there is no `mkfs.ext4`),
`HARNESS_BOOT_ARGS`, `HARNESS_GUEST_CID` (else derived per session),
`HARNESS_FIRECRACKER_LOG_LEVEL` (Warning); since M3 also `HARNESS_VM_CACHE`
(`/var/lib/secondbrain/vm`), `HARNESS_IMAGE_VERSION`,
`HARNESS_FIRECRACKER_VERSION`.

## 7. docker-compose and Dockerfile changes (proposed at M1)

What `docker-compose.yml` has today (M3 and later): `/dev/kvm` and
`/dev/vhost-vsock` under `devices:`, and `~/data/SecondBrain/vm` mounted
**read-write** at `/var/lib/secondbrain/vm` (the `HARNESS_VM_CACHE` default,
read-write so `build-base` can write `base/`; RUNBOOK-M3 §0 has the
`HARNESS_*` paths under that mount). There is no `AGENT_ENGINE`. The image
is built from the repo root (`agent-api/Dockerfile` copies
`harness/protocol` and `harness/guest` to `/root/harness`, the
`HARNESS_GUEST_ROOT`). The M1 proposal, kept for the record:

```yaml
    devices:
      - /dev/kvm:/dev/kvm
      - /dev/vhost-vsock:/dev/vhost-vsock
    environment:
      # ... existing ...
      - HARNESS_SANDBOX=firecracker
      - HARNESS_FIRECRACKER_BIN=/usr/local/bin/firecracker
      - HARNESS_KERNEL=/vm/vmlinux
      - HARNESS_ROOTFS=/vm/rootfs.ext4
      - HARNESS_WORKSPACE_TEMPLATE=/vm/workspace-template.img
    volumes:
      # ... existing ...
      - ~/data/SecondBrain/vm:/vm:ro                 # kernel, rootfs, templates (the vm/ cache; M3 base snapshot too)
      - /usr/local/bin/firecracker:/usr/local/bin/firecracker:ro   # or bake it into the image, below
```

The proposal had the `vm/` cache read-only; it became read-write in M3 for
`build-base`. Per-session files (`workspace.img`, sockets, logs, session
snapshots) still live in the session directory on the `data/` volume, never
in the cache. Firecracker needs no extra
capabilities beyond the two device nodes (it is a plain process; the jailer,
when it comes, needs `CAP_SYS_ADMIN` or a privileged sidecar - M3/OpenShell).

`agent-api/Dockerfile` - two optional additions (the base image,
`python:3.12-slim`, has no `curl`: install it in the same `RUN` or download
in a build stage):

```dockerfile
# e2fsprogs: lets the sandbox create workspace.img with mkfs.ext4 instead of
# copying HARNESS_WORKSPACE_TEMPLATE (either works; the template needs no package).
RUN apt-get update && apt-get install -y --no-install-recommends e2fsprogs && rm -rf /var/lib/apt/lists/*

# The pinned Firecracker binary, instead of the bind mount above.
ARG FIRECRACKER_VERSION=v1.17.0
ARG FIRECRACKER_SHA256=06094a1108ae9e82aa4c23a775aa92758f53f1175d422270d9d6162cb9ade558
RUN curl -fL -o /tmp/fc.tgz https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-x86_64.tgz \
 && echo "${FIRECRACKER_SHA256}  /tmp/fc.tgz" | sha256sum -c \
 && tar -xzf /tmp/fc.tgz -C /tmp \
 && install -m 0755 /tmp/release-${FIRECRACKER_VERSION}-x86_64/firecracker-${FIRECRACKER_VERSION}-x86_64 /usr/local/bin/firecracker \
 && rm -rf /tmp/fc.tgz /tmp/release-*
```

(Done since: compose and CI build with the repo root as context, and the
Dockerfile copies `harness/protocol` and `harness/guest` for
`HARNESS_GUEST_ROOT` and the local sandbox.)

Session directories inside the container are
`/root/agent-api/data/sessions/{user}/{tenant}_{session}` - short enough for
the 108-byte Unix-socket path limit. If sessions ever move deeper, the
sandbox manager already switches to paths relative to Firecracker's cwd
(`vm/`) and connects through `/proc/self/fd`; `endpoints.py`'s `bind()` of
`v.sock_{port}` would need the same treatment.

## 8. M1 exit checklist (PLAN.md section 13)

- [ ] `make image && make check` green on the server; `firecracker --version` prints the pinned version.
- [ ] Step 4: a VM boots by hand, the guest connects to `v.sock_5000`, `SendCtrlAltDel` ends the process cleanly (`firecracker.log` shows the reset, no SIGKILL needed).
- [ ] Step 5: the no-TAP VM cannot reach the network (`lo` only, connects fail, no DNS); output recorded.
- [ ] The owner's phone runs a **calendar query** (device side) and a **web search** (host side) through a Firecracker VM for the default tenant (every tenant runs the harness engine since M6).
- [ ] A redelivered `POST /api/v1/chat` with the same `input_id` runs once (one turn in `events/r0001/`, the second request attaches).
- [ ] The three goldens are reproduced live by the fake-host end-to-end test (`harness/tests`).
- [ ] agent-api tests green (`cd agent-api && uv sync && uv run pytest`; one engine since M6).
- [ ] `stop()` timing: VM gone within `stop_grace_s` (5 s) on the CtrlAltDel path; note the measured boot-to-`ready` time as the M3 baseline (< 200 ms is the snapshot target, not the rootfs-boot target).

## 9. Open questions for the server run

1. **Firecracker inside the container** (PLAN.md risks): does the plain
   `devices:` pass-through suffice on this kernel and docker version, or does
   `vhost-vsock` need the container to run with `--privileged`/`CAP_NET_ADMIN`?
   The M0 spike note should say; if not, the fallback is the sibling
   `sandbox-host` container the plan names.
2. **CID allocation**: `derive_guest_cid` hashes the session name into
   `3..2^31`. If two concurrent sessions ever collide, `PUT /vsock` fails with
   `EADDRINUSE`; the worker could then retry with `guest_cid + 1`. Decide
   whether to keep the hash or move to a host-wide counter in the worker.
3. **Stop semantics vs M3** (resolved in M3): the guest outlives a settle
   and takes the next task; after a park it returns to idle-wait
   (`guest/main.py`). `HARNESS_STOP_AT_SETTLE` defaults to `false`; the idle
   timer (`HARNESS_SESSION_IDLE_S`) parks, snapshots and destroys the VM.
   Init still reboots the VM when the guest process exits.
4. **Console log growth**: `vm/console.log` is appended for the session's
   lifetime; decide a cap or rotation with the state-sync retention.
5. **Kernel pin**: `v1.15/vmlinux-6.1.155` is the newest tag-named CI
   kernel; Firecracker `v1.17.0` runs it (the CI kernels are forward
   compatible, the device model is stable), but confirm `panic=1 reboot=k`
   behaviour and vsock on the real box; if anything is off, the 5.10 kernel in
   the same directory is the fallback (`vmlinux-5.10.245`).
6. **Workspace size**: 1 GiB sparse per session; the real cost is what the
   session writes. Confirm the `data/` volume has headroom for
   `sessions * used`.
7. **Jailer**: not in M1. If OpenShell does not bring its own runtime, the
   jailer switch (chroot, cgroups, seccomp level) is the first M3 hardening
   task and changes the launch argv only.
