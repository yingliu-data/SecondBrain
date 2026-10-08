# M3 runbook: snapshots, park and resume on the server

Follows `RUNBOOK-M1.md` (a VM boots by hand and through agent-api) and
`RUNBOOK-M2.md`. Verifies the M3 exit criteria of PLAN.md section 13 on the
owner's box (`/dev/kvm`). The build machine has no KVM: everything below was
unit-tested against a fake Firecracker API socket and runs live here for the
first time. Contracts: `docs/M3-CONTRACTS.md`; image and base snapshot:
`docs/IMAGE.md`.

Exit criteria:

1. `act()` -> `ready` < 200 ms from snapshot.
2. Park and resume a session across an hour with the same `seq`.
3. Kill the VM mid bash -> resume with `op_state{lost}`.
4. Two VMs from one snapshot produce distinct ids.
5. Engine flips to `harness` for the default tenant.

Who provides what (three workstreams, M3-CONTRACTS):

| Piece | Owner | State |
|---|---|---|
| `sandbox/firecracker.py` `restore/snapshot/has_snapshot`, base build, `harness-init`, compose devices + `vm/` volume | sandbox + image | done, unit-tested (`tests/test_sandbox_firecracker.py`, `test_sandbox_snapshot.py`, `test_sandbox_local.py`) |
| guest `--idle-wait vsock`, `--mount-workspace`, park/`parked`, log return | guest | **«guest workstream: fill in flag names / log lines if they differ»** |
| worker park/resume, `vm{op,source,ms}` host events, fallback chain, tenants.json flip | worker | **«worker workstream: fill in trigger and event names»** |

## 0. Prerequisites (once)

RUNBOOK-M1 steps 1–3 (Firecracker v1.17.0 installed, `/dev/kvm` usable,
`make image` green). Then:

```sh
sudo modprobe vhost_vsock                 # compose passes /dev/vhost-vsock; it must exist
mkdir -p ~/data/SecondBrain/vm
cd harness/image && make image && make check     # now also writes out/IMAGE_VERSION
cp out/vmlinux out/rootfs.ext4 out/workspace-template.img out/IMAGE_VERSION ~/data/SecondBrain/vm/
```

`IMAGE_VERSION` must sit **next to the rootfs** the agent-api uses: that is
how `restore()` knows which image it runs without hashing 350 MiB
(`HARNESS_IMAGE_VERSION` overrides it).

`docker-compose.yml` (this milestone) gives agent-api `/dev/kvm`,
`/dev/vhost-vsock` and mounts `~/data/SecondBrain/vm` read-write at
`/var/lib/secondbrain/vm` (= the default `HARNESS_VM_CACHE`). Environment for
the agent-api service (add to `.env` / the compose `environment:` list; not
applied by this milestone because the image must also carry the binary,
RUNBOOK-M1 §7):

```sh
HARNESS_SANDBOX=firecracker
HARNESS_FIRECRACKER_BIN=/usr/local/bin/firecracker
HARNESS_KERNEL=/var/lib/secondbrain/vm/vmlinux
HARNESS_ROOTFS=/var/lib/secondbrain/vm/rootfs.ext4
HARNESS_WORKSPACE_TEMPLATE=/var/lib/secondbrain/vm/workspace-template.img
HARNESS_VM_CACHE=/var/lib/secondbrain/vm        # default; base/ lives here
HARNESS_STOP_AT_SETTLE=false                     # a VM spans the session; park instead
```

`HARNESS_WORKSPACE_MIB` must equal the template's size (1024) when the
container has `mkfs.ext4`; otherwise images come from the template. Either
way every session's `workspace.img` must have the size the base was built
with (`MANIFEST.json workspace_bytes`), or `restore()` refuses and the worker
boots the rootfs.

## 1. Build the base snapshot

Build it **where it runs** (same Firecracker binary, same CPU; the snapshot
format is tied to the Firecracker version and the vCPU model). Inside the
container:

```sh
docker compose exec agent-api python -m app.host.sandbox.firecracker build-base
# or on the host with the out/ artifacts (then copy out/vm/base/ to the cache):
cd harness/image && make snapshot
# with explicit deployed paths:
HARNESS_VM_CACHE=~/data/SecondBrain/vm HARNESS_ROOTFS=~/data/SecondBrain/vm/rootfs.ext4 \
HARNESS_KERNEL=~/data/SecondBrain/vm/vmlinux make snapshot
```

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
docker compose exec agent-api python -m app.host.sandbox.firecracker check-base   # exit 0
ls -ls ~/data/SecondBrain/vm/base        # vmstate (KiB), mem (sparse, ~guest RSS), MANIFEST.json
tail -20 ~/data/SecondBrain/vm/.build-base/vm/console.log 2>/dev/null   # only with --keep-build-dir
```

If idle detection times out ("guest never reached idle-wait"), look at the
console (`--keep-build-dir`): the guest must log its idle-wait entry
**«guest: exact log line»** and must not have crashed on an unknown flag
(an old `guest.pyz` without `--idle-wait vsock`). `--fixed-delay 8` is the
fallback (snapshot 8 s after boot, no detection).

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
print("restore ms (launch -> load returned):", sb.last_restore_ms)
conn, _ = lst.accept()
print("guest connected after ms:", round((time.monotonic() - t0) * 1000, 1))
print("first bytes:", conn.recv(200))
sb.stop()
EOF
```

Expect `restore ms` in the tens and `guest connected` well under 200 ms
(the guest's poll is 50 ms). If `/snapshot/load` fails, the API body is in
the `SandboxError`; common causes: `workspace.img` missing in `vm/` (the
sandbox creates it; check the template), `../ipc/v.sock` already exists
(stale; the sandbox unlinks it), a Firecracker version change (rebuild base).

## 3. Exit 1: `act()` -> `ready` < 200 ms from snapshot

Two numbers, both recorded as host events in
`{session}/events/host-*.jsonl` (M3-CONTRACTS §6):

- `vm{op: restore, source: base|session, ms}`: `FirecrackerSandbox.last_restore_ms`,
  launch of the process -> `/snapshot/load` returned with the VM running.
- `act()` -> `ready`: **«worker: name of the host event / log line that
  stamps `ready` (hello acked / first guest message) and how act() start is
  stamped»**.

```sh
S=agent-api/data/sessions/owner/default_m3a
curl -sN -X POST localhost:8000/chat -H "Authorization: Bearer $API_SECRET_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"session_id": "m3a", "message": "say hi", "input_id": "i-1"}' >/dev/null
grep -h '"vm"' $S/events/host-*.jsonl | jq -c '{op: .op, source: .source, ms: .ms}'
#   {"op":"restore","source":"base","ms":31.4}
# «worker: jq for act→ready from the host events»
```

Run it 10 times on fresh sessions (`m3a1..m3a10`) and record min / median /
max. Pass: median act→ready < 200 ms with `source: base`. A
`vm{op: boot, source: rootfs}` instead means restore was refused; the reason
is in the agent-api log (`SandboxError: ...`). Baseline to compare: the M1
rootfs boot time (RUNBOOK-M1 §8).

## 4. Exit 2: park and resume across an hour, same `seq`

```sh
S=agent-api/data/sessions/owner/default_m3park
# a task that leaves the session open (the question tool, or any task followed by idle)
curl -sN -X POST localhost:8000/chat ... -d '{"session_id": "m3park", "message": "remember the number 41", "input_id": "p-1"}'
# trigger the park: «worker: idle timeout (session_idle_s) or explicit park endpoint/CLI»
```

Check after the park:

```sh
ls -ls $S/vm/snapshot/                       # vmstate, mem, META.json
jq . $S/vm/snapshot/META.json                # image_version, firecracker_version, guest_cid, created_ms
pgrep -af "firecracker.*default-m3park"      # nothing: a parked session costs no process
tail -3 $S/events/000001.jsonl | jq -c '{seq, type}'    # ... checkpoint, parked
grep -h '"vm"' $S/events/host-*.jsonl | tail -1        # {"op":"snapshot","source":"session","ms":...}
```

Wait at least an hour (do nothing; optionally restart agent-api to prove the
snapshot is on disk only), then:

```sh
curl -sN -X POST localhost:8000/chat ... -d '{"session_id": "m3park", "message": "what number?", "input_id": "p-2"}'
```

Pass:

- host event `vm{op: restore, source: session}`;
- the first guest event after resume has `seq` = parked `seq` + 1 (no gap, no
  reset to 1, no second `session_start`);
- no new `hello` was sent (resume is the transport handshake only, §3.5);
- the answer mentions 41 (the guest's in-memory state survived);
- `$S/vm/snapshot/META.json.consumed` exists (a session snapshot is
  single-use) and a later park writes a fresh `META.json`.

```sh
jq -r 'select(.seq) | .seq' $S/events/0*.jsonl | \
  python3 -c "import sys; s=[int(l) for l in sys.stdin]; print('gaps:', [(a, b) for a, b in zip(s, s[1:]) if b != a + 1])"
# gaps: []
```

## 5. Exit 3: kill the VM mid bash -> `op_state{lost}`

```sh
curl -sN -X POST localhost:8000/chat ... \
  -d '{"session_id": "m3kill", "message": "run `sleep 60; echo done` in bash", "input_id": "k-1"}' &
sleep 5
pkill -KILL -f "firecracker.*default-m3kill"
wait
```

Pass (M3-CONTRACTS §4):

- host events: `vm{op: restore, source: base}` after the death (fallback
  `vm{op: boot, source: rootfs}` is acceptable but must be noted);
- the worker sent `hello{resume: true}` («worker: where that is logged»);
- the guest fetched the log on :5001 (`get{kind: log}`) and continued
  numbering at last_seq+1, with no second `session_start`;
- an `op_state{state: lost}` for the bash call, then a `tool_result` with
  `error: operation lost (VM restarted)`, then the task completes;
- `max_resumes` = 1: killing it a second time in the same task fails the task.

```sh
jq -c 'select(.type=="op_state" or .type=="tool_result") | {seq, type, state: .state, content: .content}' \
   agent-api/data/sessions/owner/default_m3kill/events/*.jsonl
```

The workspace survives: files written before the kill are in
`vm/workspace.img` (`debugfs -R 'ls -l /' vm/workspace.img`), because the
restored VM mounts the same image.

## 6. Exit 4: two VMs from one snapshot -> distinct ids

```sh
for s in m3twin1 m3twin2; do
  curl -sN -X POST localhost:8000/chat ... -d "{\"session_id\": \"$s\", \"message\": \"create a file and tell me its name\", \"input_id\": \"t-1\"}" >/dev/null &
done; wait
for s in m3twin1 m3twin2; do
  grep -h '"vm"' agent-api/data/sessions/owner/default_$s/events/host-*.jsonl | head -1   # source: base for both
  jq -r 'select(.id) | .id' agent-api/data/sessions/owner/default_$s/events/*.jsonl | sort > /tmp/$s.ids
done
comm -12 /tmp/m3twin1.ids /tmp/m3twin2.ids | wc -l     # 0: no id appears in both
```

Pass: both restored from `base` (same `vmstate` path in each
`vm/firecracker.log`), zero shared ids (call ids, op ids, event ids — every id
is derived from session_id, M3-CONTRACTS §1), different `state_hash` in
their first checkpoints. Both VMs report the same guest CID (3) in
`vm/firecracker.log`; that is expected (userspace vsock, `docs/IMAGE.md`).

Also note from `vm/console.log` of each: no kernel `random: crng` warnings
after resume beyond the VMGenID reseed line.

## 7. Exit 5: engine flip for the default tenant

`AGENT_ENGINE` stays `legacy` globally; the default tenant's entry sets
`engine: harness` (M3-CONTRACTS §7, `tenants.json.example`
«worker workstream: confirm the key and the example diff»):

```sh
jq '.tenants.default.engine' agent-api/data/tenants.json   # "harness"
docker compose restart agent-api
# the phone: one calendar query (device side) and one web search (host side)
ls -t agent-api/data/sessions/owner/ | head -1             # a new default_* dir with events/ and vm/
```

Pass: the owner's phone works on `harness` for the default tenant, other
tenants stay on `legacy`, and the session dir shows `vm{op: restore,
source: base}`.

## 8. Things to watch

- **Disk:** each parked session holds `vm/snapshot/mem` (sparse, roughly the
  guest's touched memory, up to `HARNESS_VM_MEM_MIB`). `du -sh
  data/sessions/*/*/vm/snapshot` after a day of use; Diff snapshots are the
  follow-up if it grows.
- **workspace.img while parked:** never mount or modify a parked session's
  `workspace.img` from the host; the snapshot holds the guest's page cache
  for it and a restore over a changed image corrupts the filesystem.
  `debugfs` read-only peeks are fine.
- **Reconnect before snapshot:** after `parked` is acked the guest is back in
  idle-wait and would reconnect at once if the session's `v.sock_5000`
  listener still accepts. «worker: confirm the listener is closed (or
  accepts are refused) between the `parked` ack and `snapshot()`». Firecracker
  resets all vsock connections on snapshot, so a stray connection is closed,
  not frozen, but the guest would see a disconnect instead of a clean park.
- **Version drift:** after `docker compose pull` with a new Firecracker or a
  new image, `check-base` fails and every act() falls back to a rootfs boot
  (`vm{op: boot}`) until `build-base` runs. Parked session snapshots from the
  old version are refused too; those sessions resume via the death path
  (base restore + log return).
- **Clock:** a restored guest's clock is the snapshot's; the guest takes time
  from hello/heartbeat only. `clock_realtime` on load is deliberately not set.

## 9. Checklist

- [ ] `make image && make check` prints `IMAGE_VERSION`; copied next to the deployed rootfs.
- [ ] `build-base` detects idle via vsock metrics (`idle_detect: metrics` in MANIFEST.json); `check-base` exit 0.
- [ ] §2 by-hand restore: restore ms ____, guest connect ms ____.
- [ ] Exit 1: act→ready over 10 runs: min ____ / median ____ / max ____ ms (< 200).
- [ ] Exit 2: park ≥ 1 h, resume with contiguous `seq`, no new hello, state kept.
- [ ] Exit 3: kill mid-bash → `op_state{lost}`, error result, task completes.
- [ ] Exit 4: two VMs from base, zero shared ids, different checkpoint hashes.
- [ ] Exit 5: default tenant on `harness`, others on `legacy`, phone OK.
