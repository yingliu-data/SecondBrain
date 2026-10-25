# M3 runbook: snapshots, park and resume on the server

Follows `RUNBOOK-M1.md` (a VM boots by hand and through agent-api) and
`RUNBOOK-M2.md`. Verifies the M3 exit criteria of PLAN.md section 13 on the
owner's box (`/dev/kvm`). The build machine has no KVM: everything below was
unit-tested against a fake Firecracker API socket and runs live here for the
first time. Wire and behaviour: `docs/PROTOCOL.md` sections 5, 6 and 8, `docs/GUEST.md`
"Park, resume, bulk, manifest", `docs/HOST.md`; image and base snapshot:
`docs/IMAGE.md`.

Exit criteria:

1. `act()` -> `ready` < 200 ms from snapshot.
2. Park and resume a session across an hour with the same `seq`.
3. Kill the VM mid bash -> resume with `op_state{lost}`.
4. Two VMs from one snapshot produce distinct ids.
5. Engine flips to `harness` for the default tenant (since M6: every tenant
   runs on `harness`, §7).

Who provides what (three workstreams):

| Piece | Owner | State |
|---|---|---|
| `sandbox/firecracker.py` `restore/snapshot/has_snapshot`, base build, `harness-init`, agent-api image (firecracker + mkfs.ext4), compose env + devices + `vm/` and `image/out` volumes | sandbox + image | done, unit-tested (`tests/test_sandbox_firecracker.py`, `test_sandbox_snapshot.py`, `test_sandbox_local.py`) |
| guest `--idle-wait vsock`, `--mount-workspace DEV`, park/`parked`, log return | guest | done. Flags (`guest/main.py`): `--connect vsock --idle-wait vsock --mount-workspace /dev/vdb --log-dir /var/harness --workspace /workspace` (from `harness-init`; `--hello-timeout` 30, `--control-port` 5000, `--image-version`). Idle-wait is silent (no log line per poll); its end logs `idle-wait over: the host answered on :5000`. Then `hello for session <sid> (<user>/<tenant>)[ resume]`, on a resume `log return: N events through seq M`. Park: `checkpoint` + `parked{checkpoint_seq, reason, question_id?, wall_used_ms?, question_left_ms?}`; the wake writes `clock{kind: resumed}` first |
| worker park/resume, `vm{op,source,ms}` host events, fallback chain | worker | done. Park triggers: the idle timer (`HARNESS_SESSION_IDLE_S`, 300 s after a settle) and a question deadline when `answer_wait_s > 0` (the guest parks itself). Host log events: `vm{op: boot\|restore\|snapshot\|destroy, source: rootfs\|base\|session, ms, ready_ms, run, sandbox, ok?, error?, resume?, reason?}`, `workflow{kind: task_signal\|idle\|parked\|park_timeout\|resume\|heartbeat\|completed ...}`. No park endpoint or CLI |

Host log records are `{"ts", "hseq", "session_id", "type", "body"}` lines in
`{session}/events/host-000001.jsonl`; select by `.type` and read `.body`.
Guest events are in `{session}/events/rNNNN/000001.jsonl` (one run per fresh
guest; `r0001` for a new session, `r0000` for a migrated one).

## 0. Prerequisites (once)

RUNBOOK-M1 steps 1–3 (Firecracker v1.17.0 installed, `/dev/kvm` usable,
`make image` green). Then:

```sh
mkdir -p ~/data/SecondBrain/vm                   # HARNESS_VM_CACHE: base/ is built here
cd harness/image && make image && make check     # out/: vmlinux rootfs.ext4 workspace-template.img IMAGE_VERSION
cd ../.. && docker compose build agent-api       # or pull the CI image
docker compose up -d agent-api
docker compose exec agent-api firecracker --version   # Firecracker v1.17.0 (versions.env pin)
docker compose exec agent-api mkfs.ext4 -V
```

`IMAGE_VERSION` must sit **next to the rootfs** the agent-api uses: that is
how `restore()` knows which image it runs without hashing 350 MiB
(`HARNESS_IMAGE_VERSION` overrides it).

The agent-api image carries `/usr/local/bin/firecracker` (+ `jailer`), both
from the `versions.env` pin and sha256-verified at build time, and
`mkfs.ext4`. `docker-compose.yml` sets the sandbox up already: it passes
`/dev/kvm` (no `/dev/vhost-vsock`: Firecracker's vsock is userspace over
Unix sockets), mounts `harness/image/out` read-only at `/opt/harness-image`
and `~/data/SecondBrain/vm` read-write at `/var/lib/secondbrain/vm`, and sets

```sh
HARNESS_SANDBOX=firecracker                      # ${HARNESS_SANDBOX:-firecracker}; `local` = guest as a subprocess
HARNESS_KERNEL=/opt/harness-image/vmlinux
HARNESS_ROOTFS=/opt/harness-image/rootfs.ext4
HARNESS_WORKSPACE_TEMPLATE=/opt/harness-image/workspace-template.img   # only used without mkfs.ext4
HARNESS_VM_CACHE=/var/lib/secondbrain/vm         # base/ lives here
# unset, defaults: HARNESS_FIRECRACKER_BIN=firecracker (on PATH)
# (a VM spans the session: the idle timer parks it, it never stops at a settle)
```

A box without KVM (dev laptop, CI): `HARNESS_SANDBOX=local` in `.env` plus a
`docker-compose.override.yml` (auto-loaded; do not commit it) that drops the
device, since compose refuses to start with a missing one:

```yaml
services:
  agent-api:
    devices: !reset []
```

`HARNESS_WORKSPACE_MIB` must equal the template's size (1024) when the
container has `mkfs.ext4`; otherwise images come from the template. Either
way every session's `workspace.img` must have the size the base was built
with (`MANIFEST.json workspace_bytes`), or `restore()` refuses and the worker
boots the rootfs.

Shell helpers for the steps below (from the compose project root).
agent-api publishes no host port and its image has no `curl`, and every
`/api/v1/*` route wants Bearer + `X-Timestamp` + `X-Signature` (HMAC-SHA256
of timestamp + body with the tenant's key, `app/auth/middleware.py`), so the
requests run inside the container:

```sh
api() {   # api PATH [JSON]: GET without a body, POST with one; the response streamed
  docker compose exec -T -e KEY="${KEY:-}" -e MAX_TIME="${MAX_TIME:-}" agent-api python - "$@" <<'EOF'
import hashlib, hmac, os, sys, time, urllib.request
key = os.environ.get("KEY") or os.environ["API_SECRET_KEY"]   # KEY=<tenant key> for another tenant
path, body = sys.argv[1], (sys.argv[2].encode() if len(sys.argv) > 2 else b"")
ts = str(int(time.time()))
sig = hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://localhost:8000" + path, data=body or None, headers={
    "Authorization": f"Bearer {key}", "X-Timestamp": ts, "X-Signature": sig,
    "Content-Type": "application/json"})
end = time.monotonic() + float(os.environ.get("MAX_TIME") or "inf")
with urllib.request.urlopen(req) as r:
    for line in r:                  # SSE keepalives arrive every 15 s
        sys.stdout.write(line.decode()); sys.stdout.flush()
        if time.monotonic() > end:
            break                   # drop the client; the task runs on
EOF
}
chat() { api /api/v1/chat "$1"; }
# the default tenant's user (sessions live in agent-api/data/sessions/$U/default.<session_id>)
U=$(jq -r '[.tenants[] | select(.is_default)][0] | .user // .name // "default"' agent-api/data/tenants.json)
hostev() { jq -c "select(.type==\"$2\") | .body" "$1"/events/host-*.jsonl; }   # hostev $S vm
```

## 1. Build the base snapshot

Build it **where it runs** (same Firecracker binary, same CPU; the snapshot
format is tied to the Firecracker version and the vCPU model). Inside the
container:

```sh
docker compose exec agent-api python -m app.host.sandbox.base_snapshot build-base
# or on the host with the out/ artifacts (then copy out/vm/base/ to the cache):
cd harness/image && make snapshot
# with explicit deployed paths:
HARNESS_VM_CACHE=~/data/SecondBrain/vm make snapshot   # kernel/rootfs default to out/, as in compose
```

`build-base` flags: `--cache DIR` (default `HARNESS_VM_CACHE`),
`--idle-timeout S` (120), `--settle S` (1.0, after idle is detected),
`--fixed-delay S` (skip detection), `--keep-build-dir`.

Expected stderr:

```
[snapshot] booting rootfs in /var/lib/secondbrain/vm/.build-base
[snapshot] booted in 35.2 ms; waiting for the guest's idle-wait
[snapshot] vsock activity 0 (streak 0/3)        # kernel + init + python start
...
[snapshot] vsock activity 8 (streak 1/3)        # guest idle-wait connect attempts (RST)
[snapshot] vsock activity 8 (streak 3/3)
[snapshot] base snapshot written to /var/lib/secondbrain/vm/base in 900.0 ms
```

and `MANIFEST.json` on stdout. Check it:

```sh
docker compose exec agent-api python -m app.host.sandbox.base_snapshot check-base   # exit 0, the manifest on stdout
ls -ls ~/data/SecondBrain/vm/base        # vmstate (KiB), mem (sparse, ~guest RSS), MANIFEST.json
tail -20 ~/data/SecondBrain/vm/.build-base/vm/console.log 2>/dev/null   # only with --keep-build-dir
```

If idle detection times out (`[snapshot] error: guest never reached
idle-wait within 120.0s (no vsock connect attempts; see .../console.log);
retry with a fixed delay`), look at the console (`--keep-build-dir`). Init
logs `harness-init: starting guest harness <image version> (idle-wait, then vsock -> CID 2)`
just before it starts the guest; the guest's idle-wait itself is silent (no
log line per 5 ms poll; `idle-wait over: the host answered on :5000` only
appears once a host listener answers, never during a build). What must not
be there is a Python traceback or `guest.main: error: unrecognized
arguments` (an old `guest.pyz` without `--idle-wait vsock`) followed by
`harness-init: guest exited with 2`. `--fixed-delay 8` is the fallback
(snapshot 8 s after boot, no detection; `idle_detect: fixed` in the manifest).

Rebuild after every `make image`, Firecracker upgrade, or change of
`HARNESS_VM_MEM_MIB`, `HARNESS_VM_VCPUS`, `HARNESS_WORKSPACE_MIB`. A rebuild
swaps `base/` atomically; running VMs are unaffected.

## 2. Restore by hand (before involving the worker)

Same as RUNBOOK-M1 §4 but from the snapshot. One-liner through the sandbox
class, with a stand-in listener so the guest leaves idle-wait:

```sh
docker compose exec agent-api python - <<'EOF'
import socket, time, os
from pathlib import Path
from app.host.sandbox.firecracker import FirecrackerSandbox
sd = Path("/tmp/m3-restore/default_r1"); (sd / "ipc").mkdir(parents=True, exist_ok=True)
lst = socket.socket(socket.AF_UNIX); p = sd / "ipc" / "v.sock_5000"
p.unlink(missing_ok=True); lst.bind(str(p)); lst.listen(1)
sb = FirecrackerSandbox()
t0 = time.monotonic(); sb.restore(sd)
print("restore:", sb.last_vm_event)           # {'op': 'restore', 'source': 'base', 'ms': ...}
conn, _ = lst.accept()
print("guest connected after ms:", round((time.monotonic() - t0) * 1000, 1))
# the guest sends nothing yet: idle-wait ends with the host's first line
conn.close(); sb.stop()
EOF
```

Expect `ms` in the tens and `guest connected` well under 200 ms
(the guest's poll is 5 ms). If `/snapshot/load` fails, the API body is in
the `SandboxError`; common causes: `workspace.img` missing in `vm/` (the
sandbox creates it; check the template), `../ipc/v.sock` already exists
(stale; the sandbox unlinks it), a Firecracker version change (rebuild base).

## 3. Exit 1: `act()` -> `ready` < 200 ms from snapshot

Two numbers, both in the host event `vm{op: restore, source: base}` of
`{session}/events/host-*.jsonl` (`Worker._vm_event`):

- `ms`: the sandbox's own figure (`FirecrackerSandbox.last_vm_event["ms"]`),
  launch of the process -> `/snapshot/load` returned with the VM running.
- `ready_ms`: the host's, from the start of the guest start in
  `ensure_ready()` (the first thing a task does) to the guest's control link
  up, the session's MCP tool list fetched from the gateway and `hello` sent.
  This is act() -> ready. It includes the gateway's `GET /internal/tools`
  (bounded by `MCP_GATEWAY_TOOLS_TIMEOUT_S`, 5 s): a high `ready_ms` with a
  low `ms` points at the gateway, not the VM.

The task itself is stamped by `workflow{kind: task_signal, input_id}` (the
direct path: just before `ensure_ready`) or `workflow{kind: activity_start,
activity: prepare_vm}` (the Temporal path); their `ts` against the `vm`
event's `ts` (millisecond ISO stamps) is the end-to-end figure.

```sh
S=agent-api/data/sessions/$U/default_m3a
chat '{"session_id": "m3a", "message": "say hi", "input_id": "i-1"}' >/dev/null
hostev $S vm | jq -c '{op, source, ms, ready_ms}'
#   {"op":"restore","source":"base","ms":31,"ready_ms":120}
python3 - $S <<'EOF'
import glob, json, sys
from datetime import datetime
recs = [json.loads(l) for p in sorted(glob.glob(sys.argv[1] + "/events/host-*.jsonl")) for l in open(p)]
t = lambda r: datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
start = next(r for r in recs if r["type"] == "workflow"
             and r["body"].get("kind") in ("task_signal", "activity_start"))
vm = next(r for r in recs if r["type"] == "vm" and r["body"]["op"] in ("restore", "boot"))
print("task -> ready ms:", round((t(vm) - t(start)).total_seconds() * 1000), vm["body"])
EOF
```

Run it 10 times on fresh sessions (`m3a1..m3a10`) and record min / median /
max of `ready_ms`. Pass: median < 200 ms with `source: base`. A
`vm{op: boot, source: rootfs}` instead means restore was refused; the reason
is in the agent-api log (`session <user>/default.<sid>: base restore failed
(...)`). Baseline to compare: the M1 rootfs boot time (RUNBOOK-M1 §8).

## 4. Exit 2: park and resume across an hour, same `seq`

There is no park endpoint or CLI. A session parks (a) on the idle timer:
`HARNESS_SESSION_IDLE_S` (default 300 s) after a settle the worker sends
`park`, snapshots and destroys the VM; or (b) when a question's deadline
(`HARNESS_QUESTION_TIMEOUT_S`, 300 s) passes with the tenant's
`answer_wait_s > 0` (the default, 86400): the guest parks itself. For this
test set `HARNESS_SESSION_IDLE_S=60` on the agent-api service
(`docker compose up -d agent-api` after editing the environment), or wait the
30 minutes.

```sh
S=agent-api/data/sessions/$U/default_m3park
chat '{"session_id": "m3park", "message": "remember the number 41", "input_id": "p-1"}'
sleep 90           # > HARNESS_SESSION_IDLE_S: the idle park
```

Check after the park:

```sh
ls -ls $S/vm/snapshot/                       # vmstate, mem, META.json
jq . $S/vm/snapshot/META.json                # format, image_version, firecracker_version, guest_cid, session, created_ms
jq . $S/vm/parked.json                       # cseq, reason: "idle", checkpoint_seq, last_seq, at
pgrep -af "firecracker.*default-m3park"      # nothing: a parked session costs no process
tail -3 $S/events/r0001/0*.jsonl | jq -c '{seq, type, body}'   # ... checkpoint, parked{checkpoint_seq, reason: "idle"}
hostev $S workflow | tail -2                 # {"kind":"idle","idle_s":60.0} {"kind":"parked","reason":"idle","checkpoint_seq":N,"snapshot":true}
hostev $S vm | tail -2                       # {"op":"snapshot","source":"session","ok":true,...} {"op":"destroy","reason":"idle",...}
```

A park during a task (a question park) also carries `wall_used_ms` (the
task's wall clock used so far) and `question_left_ms` (`{question_id: ms
left}`) in `parked`; an idle park between tasks has neither.

Wait at least an hour (do nothing; optionally restart agent-api to prove the
snapshot is on disk only: the new Worker reads `vm/parked.json`), then:

```sh
chat '{"session_id": "m3park", "message": "what number?", "input_id": "p-2"}'
```

Pass:

- host event `vm{op: restore, source: session}` (no `resume` key);
- the first guest event after resume is `clock{kind: resumed}` with `seq` =
  parked `seq` + 1 (no gap, no reset to 1, no second `session_start`, no
  new `r0002/`), then the `input` of p-2;
- no new `hello` was sent (resume is the transport handshake only, §3.5): no
  `vm` event with `resume: true`, and the console shows no second
  `hello for session` line;
- the answer mentions 41 (the guest's in-memory state survived);
- `$S/vm/snapshot/META.json.consumed` exists (a session snapshot is
  single-use) and a later park writes a fresh `META.json`.

A refused session snapshot is `vm{op: restore, source: session, ok: false,
error}` followed by the fallback `vm{op: restore, source: base, resume:
true}` (hello{resume: true} + the log return); still a pass for continuity
(same run, contiguous seq), but note it.

```sh
jq -r 'select(.seq) | .seq' $S/events/r0001/0*.jsonl | \
  python3 -c "import sys; s=[int(l) for l in sys.stdin]; print('gaps:', [(a, b) for a, b in zip(s, s[1:]) if b != a + 1])"
# gaps: []
docker compose exec agent-api python -m protocol.replay verify data/sessions/$U/default_m3park
# verified 1 target(s): 1 ok, 0 failed
```

## 5. Exit 3: kill the VM mid bash -> `op_state{lost}`

```sh
chat '{"session_id": "m3kill", "message": "run `sleep 60; echo done` in bash", "input_id": "k-1"}' &
sleep 5
pkill -KILL -f "firecracker.*default-m3kill"
wait
```

Pass (`docs/PROTOCOL.md` section 6):

- host events: `workflow{kind: resume, reason: guest_died, resumes: 1,
  acked_seq: N}`, then `vm{op: restore, source: base, resume: true}`
  (fallback `vm{op: boot, source: rootfs, resume: true}` is acceptable but
  must be noted);
- the worker sent `hello{resume: true}`: that is the `resume: true` on the
  `vm` event (the hello itself is not logged on the host); the guest's
  console shows `hello for session m3kill (<user>/default) resume`;
- the guest fetched the log on :5001 (`get{kind: log}`; console:
  `log return: N events through seq N`) and continued numbering at
  last_seq+1, with no second `session_start`;
- an `op_state{state: lost}` for the bash call, then a `tool_result` with
  `error: operation lost (VM restarted)`, then the task completes;
- `HARNESS_MAX_RESUMES` = 1: killing it a second time in the same task fails
  the task (`The agent sandbox failed: the guest died again after a resume`).

```sh
hostev agent-api/data/sessions/$U/default_m3kill workflow | grep resume
jq -c 'select(.type=="op_state" or .type=="tool_result") | {seq, type, state: .body.state, content: .body.content}' \
   agent-api/data/sessions/$U/default_m3kill/events/r0001/*.jsonl
grep -E 'hello for session|log return' agent-api/data/sessions/$U/default_m3kill/vm/console.log
```

The workspace survives: files written before the kill are in
`vm/workspace.img` (`debugfs -R 'ls -l /' vm/workspace.img`), because the
restored VM mounts the same image.

## 6. Exit 4: two VMs from one snapshot -> distinct ids

```sh
for s in m3twin1 m3twin2; do
  chat "{\"session_id\": \"$s\", \"message\": \"create a file and tell me its name\", \"input_id\": \"t-1\"}" >/dev/null &
done; wait
for s in m3twin1 m3twin2; do
  D=agent-api/data/sessions/$U/default_$s
  hostev $D vm | head -1                                   # source: base for both
  jq -r '.body | to_entries[] | select((.key | endswith("_id")) and .key != "input_id") | .value | strings' \
     $D/events/r0001/*.jsonl | sort -u > /tmp/$s.ids
done
comm -12 /tmp/m3twin1.ids /tmp/m3twin2.ids | wc -l     # 0: no id appears in both
docker compose exec agent-api python -m protocol.replay replay \
  data/sessions/$U/default_m3twin1 data/sessions/$U/default_m3twin2   # two different state_hash=
```

Pass: both restored from `base` (same `vmstate` path in each
`vm/firecracker.log`), zero shared ids (call ids, turn ids, question ids —
every id is derived from session_id, `docs/GUEST.md` "Snapshot point"; the client-chosen
`input_id` is excluded), different `state_hash` in the replay summaries
(checkpoints are only written at a park, so compare the fold). Both VMs
report the same guest CID (3) in `vm/firecracker.log`; that is expected
(userspace vsock, `docs/IMAGE.md`).

Also note from `vm/console.log` of each: no kernel `random: crng` warnings
after resume beyond the VMGenID reseed line.

## 7. Exit 5: every tenant on `harness`

Superseded by M6 (`RUNBOOK-M6.md` §3): `AGENT_ENGINE` and the legacy loop are
gone, every tenant runs the harness engine, and an `engine` key in
`tenants.json` is ignored with a startup warning
(`[tenants] WARNING: tenant default: \`engine: harness\` ignored (every tenant
runs the harness engine)`). What remains to check here is the phone:

```sh
docker compose restart agent-api
docker compose logs agent-api | grep -E '\[tenants\]'         # Tenants loaded: [...], any `ignored` warnings
# the phone: one calendar query (device side) and one web search (host side)
ls -t agent-api/data/sessions/$U/ | head -1                   # a default_* dir with events/r0001 and vm/
```

Pass: the owner's phone works, and the session dir shows `vm{op: restore,
source: base}`.

## 8. Things to watch

- **Disk:** each parked session holds `vm/snapshot/mem` (sparse, roughly the
  guest's touched memory, up to `HARNESS_VM_MEM_MIB`). `du -sh
  agent-api/data/sessions/*/*/vm/snapshot` after a day of use; Diff
  snapshots are the follow-up if it grows.
- **workspace.img while parked:** never mount or modify a parked session's
  `workspace.img` from the host; the snapshot holds the guest's page cache
  for it and a restore over a changed image corrupts the filesystem.
  `debugfs` read-only peeks are fine.
- **Reconnect before snapshot:** after `parked` is acked the guest is back in
  idle-wait and would reconnect at once if the session's `v.sock_5000`
  listener still accepted. The worker closes the `:5000`, `:5001` and
  `:5011` listeners (and unlinks their `v.sock_*` files) when the `parked`
  event arrives, before it acks it (`Worker._on_event` ->
  `_close_guest_ports`), and reopens them right before a restore or boot.
  Firecracker resets all vsock connections on snapshot, so a stray
  connection would be closed, not frozen.
- **Version drift:** after `docker compose pull` with a new Firecracker or a
  new image, `check-base` fails and every act() falls back to a rootfs boot
  (`vm{op: boot}`) until `build-base` runs. Parked session snapshots from the
  old version are refused too; those sessions resume via the fallback (base
  restore + `hello{resume: true}` + log return).
- **Clock:** a restored guest's clock is the snapshot's. The guest writes
  `clock{kind: resumed}` first on every wake from a park (the only event that
  ends a parked stretch) and learns host time from `hello.at` and heartbeats
  (`host_hello` / `host_heartbeat` clock events). Parked hours never count
  against a task's wall clock or a question's deadline (`parked.wall_used_ms`,
  `parked.question_left_ms`). `clock_realtime` on load is deliberately not set.

## 9. Checklist

- [ ] `make image && make check` prints `IMAGE_VERSION`; copied next to the deployed rootfs.
- [ ] `build-base` detects idle via vsock metrics (`idle_detect: metrics` in MANIFEST.json); `check-base` exit 0.
- [ ] §2 by-hand restore: restore ms ____, guest connect ms ____.
- [ ] Exit 1: `ready_ms` over 10 runs: min ____ / median ____ / max ____ ms (< 200).
- [ ] Exit 2: park ≥ 1 h, resume with contiguous `seq` (`clock{resumed}` at parked + 1), no new hello, state kept, `verify` clean.
- [ ] Exit 3: kill mid-bash → `op_state{lost}`, error result, task completes.
- [ ] Exit 4: two VMs from base, zero shared ids, different state hashes.
- [ ] Exit 5: every tenant on `harness` (M6), phone OK.
