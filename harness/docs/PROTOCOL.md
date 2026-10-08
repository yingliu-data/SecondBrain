# vsock control protocol

The contract between the guest harness and the host worker (PLAN.md section 4).
Implemented by `protocol/messages.py` (envelopes, acks, resend bookkeeping);
the guest transport (`guest/transport`) and the host vsock server drive it.

## 1. Channels

| Port | Direction | Content |
|---|---|---|
| :5000 control | guest → host connect; both directions after | control messages, events, acks, live messages — JSONL |
| :5001 bulk | guest → host connect; both directions after | spill files, workspace manifest + files at settle, log return on resume |
| :5010 LLM | guest → host | HTTP/1.1 to the LLM proxy, SSE |
| :5011 gateway | guest → host | HTTP/1.1, MCP streamable HTTP to the gateway forwarder |
| 5020–5029 | — | reserved |

The guest never listens; it connects out to CID 2. No web proxy: the guest has
no network device; `web_search` and `webfetch` are host-side skills. Dev
transport: Unix sockets named after the ports, with a Docker container as the
guest, used until the microVM image exists.

## 2. Envelopes on :5000

One JSON object per line. Every line carries a `type`; `parse(line)` classifies
it as one of four kinds.

### Event (guest → host, sequenced by `seq`)

The events themselves are the messages. `ts` is data, never a fold input;
`schema` is the event schema version.

```json
{"session_id":"sess-1","seq":1,"ts":"2026-10-07T12:00:00Z","schema":1,"type":"session_start","body":{"user":"u","tenant":"t"}}
```

A `tool_call` event whose `side` is not `guest` is also a request to the host:
the worker runs or relays it and answers with `side_result`.

### Control message (host → guest, sequenced by `cseq`)

Types: `hello`, `task`, `follow_up`, `steer`, `ui_response`, `side_result`,
`park`, `abort`, `heartbeat`.

```json
{"session_id":"sess-1","cseq":2,"type":"task","body":{"input_id":"in-7","text":"Summarise my week","attachments":[]}}
```

| type | body |
|---|---|
| `hello` | `config` (model alias, limits, tools with sides, hooks, `async_tools`, `question_timeout_s`, `ui_attached`), `session_id`, `principal` (user, tenant), `system_block`, `resume`, `at` |
| `task` / `follow_up` / `steer` | `input_id`, `text`, `attachments` |
| `ui_response` | `input_id`, `question_id`, `answer` |
| `side_result` | `call_id`, `side` (`host`/`device`/`mcp`), `content`, `ok`; optional `memory` `{scope, op, slug, sha256}`: the memory the call changed (`remember`/`forget`), which the guest records as a `memory` event after the ok `tool_result` (docs/GUEST.md) |
| `park` | `reason` |
| `abort` | `reason` |
| `heartbeat` | `at` — every 60 s → `clock{host_heartbeat}` |

### Ack (either direction)

Exactly one of `ack_seq` (host acks events) or `ack_cseq` (guest acks control
messages). A line with both, or neither, is rejected.

```json
{"type":"ack","session_id":"sess-1","ack_seq":12}
```

### Live message (guest → host, unsequenced, never stored)

Types: `ready`, `delta`, `log`, `bulk_ref`. No `seq`, no `session_id`.

```json
{"type":"delta","body":{"session_id":"sess-1","turn_id":"turn_3f2a","kind":"content","index":0,"text":"Here is"}}
```

## 3. The ack rule

1. `seq` and `cseq` are independent counters. Each starts at 1, increases by
   one per message, and belongs to the session: it survives reconnects and VM
   restores and is never reused.
2. An ack carries the highest contiguous number delivered. A receiver that has
   delivered 1, 2 and 4 acks 2.
3. Duplicates (number ≤ last delivered) are dropped. A number beyond
   last + 1 is a gap: it is not delivered; the receiver keeps acking the
   contiguous prefix and waits for the resend.
4. The host acks an event only after the state sync has appended it to the
   session directory. The guest log is a buffer; the host is the canonical
   store.
5. A sender keeps every message until it is acked and discards acked ones. An
   ack higher than anything sent is a protocol error.

## 4. Reconnect

1. The stream drops. The coordinator pauses; the transport reconnects with
   backoff.
2. On the new connection the guest's first line is its handshake: an ack of
   the highest contiguous `cseq` it has delivered.
3. The host answers with an ack of the highest contiguous `seq` it has stored.
4. Both sides resend, in order, everything they hold numbered above the
   peer's ack (`resend_plan`), then continue normally.
5. The receiver applies the duplicate/gap rules of section 3 to what arrives.

## 5. Park and resume

1. The worker sends `park{reason}`.
2. The guest finishes its in-flight guest-side operations (a park is honoured
   only at a turn boundary or while every running operation is host-originated),
   writes a `checkpoint` event, emits `parked`, and stops connecting.
3. The worker snapshots the VM.
4. Resume: the worker restores the snapshot; the guest reconnects (section 4).
5. The worker sends its pending control messages, `ui_response` first.

## 6. Guest or VM death

1. The worker restores the base snapshot on the same `workspace.img`.
2. The worker sends `hello{resume: true}`.
3. The worker streams the acked event log back to the guest on :5001.
4. Open calls (every side) are recorded `op_state{lost}` with an error result.
5. `max_resumes` is 1.

### As built in M3 (guest side)

- Park: the guest also parks while only host-originated operations or open
  questions are pending. Running guest operations get `park_grace_s`, then are
  killed (`op_state{killed}`). After `checkpoint` + `parked` are acked the
  guest closes :5000 and polls it every 50 ms: **the worker must stop
  accepting :5000 connections when it receives `parked` (before acking it)**,
  or the guest reconnects at once. Resume is an ordinary reconnect (section 4).
- Death: a fresh guest sends no handshake (it has no session yet); its first
  line is the ack of the resume hello's cseq. It takes that cseq as its floor:
  controls numbered below it are never delivered to it (the host should treat
  them as delivered and re-send anything that still matters, e.g. a task not
  yet in the log, with new cseqs after the hello). An `ack_seq` the host sends
  before the guest has fetched the log is ignored (logged). After the log
  return the guest's next event is the returned `last_seq + 1`.

## 7. Bulk channel (:5001)

Framing: `protocol/bulk.py`. The guest connects; one transfer per connection;
every line is UTF-8 JSON + `"\n"`, at most 64 KiB.

| op | guest sends | host answers |
|---|---|---|
| `put` | `{"op":"put","session_id","kind","path","bytes","sha256"}` then exactly `bytes` raw bytes | `{"ok":true}` or `{"ok":false,"error"}` after verifying bytes and sha256 |
| `get` | `{"op":"get","session_id","kind":"log","after_seq"}` | `{"ok":true,"bytes","sha256","last_seq"}` then exactly `bytes` of JSONL (the acked events with seq > after_seq, in order), or `{"ok":false,"error"}` |

Put kinds and where the host stores them: `spill` (path `.spill/<call_id>.txt`
→ `workspace/.spill/`), `workspace` (the file's path under /workspace →
`workspace/`), `manifest` (`manifest.json` → `{session}/manifest.json`, beside
`workspace/`, not inside it). The wire paths are `protocol.bulk.SPILL_DIR` and
`protocol.bulk.MANIFEST_PATH`. Paths
are relative with no empty, `.` or `..` part.

Who uses it: a spill file is put when the tool result is produced (before its
`bulk` event); at settle every changed workspace file under 8 MiB, then the
manifest (`{"version":1,"files":[{path,bytes,sha256,mode}],"total_bytes"}`,
sorted by path, canonical JSON); on a death resume one `get` with
`after_seq: 0`. The guest checks a `get` payload's byte count, sha256 and that
its seqs run gapless from `after_seq + 1` to `last_seq` (an empty log must
answer `last_seq` = `after_seq`). A host that refuses a `put` header may reply
and close without reading the payload; the guest reads the reply even when
its payload write fails.
