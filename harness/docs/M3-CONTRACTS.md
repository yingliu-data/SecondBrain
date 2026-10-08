# M3 contracts — snapshots, park and resume

Fixed before the M3 work split across three workstreams (guest, sandbox+image,
host worker). Each workstream owns its files; these contracts are the seams.

## 1. Snapshot point (guest ⇄ image ⇄ sandbox)

- The base snapshot is taken with the guest process **running and idle before
  it connects out**: `guest.main --idle-wait vsock` loops trying to connect to
  :5000 (CID 2) every `IDLE_POLL_S` (50 ms), silently. At snapshot-build time no
  host listener exists, so every attempt fails. After restore, the per-session
  listener exists and the first attempt succeeds → normal hello.
- `/workspace` is **not mounted** in the base snapshot. `harness-init` leaves
  /dev/vdb alone; the guest mounts `/dev/vdb` on `/workspace` itself *after*
  idle-wait ends and before it reads hello (`guest.main --mount-workspace
  /dev/vdb`). A cold rootfs boot uses the same path, so there is one flow.
- Nothing random is drawn before the snapshot point; every id is derived
  (`canonical.derive_id`). Two VMs restored from one snapshot differ only by
  what hello tells them (session_id), so their ids and logs differ.

## 2. Sandbox API (agent-api/app/host/sandbox/__init__.py)

`start(session_dir)`, `stop()`, `is_alive()` as M1, plus
`restore(session_dir, snapshot_dir=None)`, `snapshot(snapshot_dir)`,
`has_snapshot(snapshot_dir=None)`. Base snapshot location:
`HARNESS_VM_CACHE` (default `/var/lib/secondbrain/vm`) → `base/{vmstate,mem}`
plus `base/MANIFEST.json` {image_version, kernel sha, rootfs sha, firecracker
version}. Per-session snapshot: `{session_dir}/vm/snapshot/{vmstate,mem,META.json}`.
Snapshots record paths **relative** to the Firecracker process cwd (= the
session's `vm/` dir), so one base snapshot serves every session (its drive
`workspace.img` and vsock `../ipc/v.sock` resolve per session).
`LocalSandbox` emulates restore/snapshot (restore = start a guest process;
snapshot = wait for the process to exit after `parked`).

## 3. Park (worker → guest)

1. Worker sends control `park{reason}`.
2. Guest honours it at a turn boundary, or immediately when every running
   operation is host-originated (host/device/mcp). Running **guest** ops get
   `park_grace_s` (limits, default 30) to finish; then they are killed and
   recorded `op_state{killed}`.
3. Guest emits `checkpoint{through_seq, state_hash, prev_hash}` then
   `parked{checkpoint_seq, reason}`, waits until both are acked
   (`transport.wait_acked`), closes its sockets and **returns to idle-wait**
   (`--idle-wait vsock` loop), keeping in-memory state. It does NOT exit.
4. Worker sees the acked `parked`, calls `sandbox.snapshot({session}/vm/snapshot)`.
5. Resume: `sandbox.restore(session_dir, {session}/vm/snapshot)`; the guest's
   idle loop connects; transport handshake (ack of cseq) + resend as M1 §4;
   no new hello; worker sends pending controls, `ui_response` first. The guest
   emits nothing special on resume; the next control wakes the coordinator.
   `seq` and `cseq` continue unchanged.

## 4. Death and log return (worker → guest, :5001)

1. Worker detects VM/guest death mid-task (not after `parked`).
2. `sandbox.restore(session_dir)` from the BASE snapshot (fallback `start`), same
   `workspace.img`. `max_resumes` = 1 per task.
3. Worker sends `hello{resume: true}` (cseq continues; the guest learns its
   cseq floor from hello's own cseq: it accepts the hello's cseq as the next
   expected).
4. Guest does a bulk `get{kind: log, after_seq: 0}` on :5001 (framing in
   `protocol/bulk.py`), folds the returned events into its state and event
   log (buffer), sets its next seq = last_seq+1. It does **not** emit a second
   `session_start` (schema error). It emits `op_state{lost}` for every call open in the folded state,
   then continues the task (the coordinator wakes with `wake{reason: tool_result}`
   for the lost results so the model sees error results).
5. Lost ops produce `tool_result{content: "error: operation lost (VM restarted)"}`.

## 5. Settle manifest (guest → host, :5001)

At settle the guest walks `/workspace` (excluding `.spill/`, `.git/`), builds
`protocol.bulk.manifest(files)`, puts it with kind `manifest`, and puts each
changed file with kind `workspace` (files under 8 MiB; larger ones listed in
the manifest only). `settle.manifest` = the manifest dict. Spill files go as
kind `spill` when they are created (bulk event already exists in M1/M2).

## 6. Host events (worker)

`vm{op: restore|snapshot|destroy|boot, source: base|session|rootfs, ms}` host
events in `events/host-*.jsonl` via `StateSync.host_event`.

## 7. Engine default

`AGENT_ENGINE` stays the global env default (`legacy`); the default tenant's
entry in tenants.json may set `engine: harness`; `Tenant.engine` wins over the
env. M3 flips the default tenant in `tenants.json.example`.
