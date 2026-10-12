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
{"session_id":"sess-1","cseq":2,"type":"task","body":{"input_id":"in-7","text":"Summarise my week","attachments":[],"wall_clock_s":600}}
```

| type | body |
|---|---|
| `hello` | `config` (model alias, limits, tools with sides, ports, `async_tools`, `question_timeout_s`, `ui_attached`, `harness_version`), `session_id`, `principal` (user, tenant), `system_block`, `resume`, `at` |
| `task` / `follow_up` / `steer` | `input_id`, `text`, `attachments`, `wall_clock_s?` (int ≥ 1: the task's wall clock; the guest arms it for the task the input starts, logs it on the `input` event and settles `timeout` at it; absent → hello `limits.wall_clock_s`) |
| `ui_response` | `input_id`, `question_id`, `answer` |
| `side_result` | `call_id`, `side` (`host`/`device`/`mcp`), `content`, `ok`; optional `memory` `{scope, op, slug, sha256}`: the memory the call changed (`remember`/`forget`), which the guest records as a `memory` event after the ok `tool_result` (docs/GUEST.md) |
| `park` | `reason` |
| `abort` | `reason`: `escalated` → `settle{escalated}`; any other → `settle{aborted, answer: ""}`; `detail` carries the reason |
| `heartbeat` | `at` — every 60 s → `clock{host_heartbeat}` |

`hello.config.limits` (unknown keys are rejected; every default is
`protocol.limits.DEFAULT_LIMITS`) also carries `park_grace_s` (default 30)
and `answer_wait_s` (int ≥ 0, default 0; section 5). `hello.config.ports`
names the guest's ports (`control`, `bulk`, `llm`, `gateway`; defaults
`protocol.ports.PORTS`: 5000, 5001, 5010, 5011); an unknown port name is
logged and ignored. `hello.config.harness_version` is the host's
`protocol.version.HARNESS_VERSION`; the guest records its own in
`session_start.harness_version` and the host logs a `harness_skew` host
event when they differ.

Each `hello.config.tools` entry is
`{name, side, pin, schema?, description?, timeout_s?}`. Host and device
entries come from the skill registry (`pin: null`); mcp entries are the
gateway's `GET /internal/tools` answer, verbatim:

```json
{"name": "wcc-events__draft_event", "side": "mcp", "pin": "<64 hex>", "timeout_s": 2700,
 "description": "...", "schema": {"type": "function", "function": {"name", "description", "parameters"}}}
```

The meta tools (`search_tools`, `describe_tool`, `call_tool`) get `pin` =
the sha256 of their own canonical definition and `timeout_s` = the maximum
over the tenant's upstreams. The guest keeps `schema` and `timeout_s` per
name (docs/GUEST.md "MCP over :5011"); `session_start.tools` records only
`{name, side, pin}`.

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
2. The guest honours it at a turn boundary: when no provider call is in
   flight and no guest operation runs. Host-, device- and mcp-side calls and
   open questions do not hold a park. Guest operations still running after
   `park_grace_s` are killed (`op_state{killed}`).
3. The guest writes `checkpoint{through_seq, state_hash, prev_hash}` and
   `parked{checkpoint_seq, reason}`, waits until both are acked, closes :5000
   and returns to idle-wait (it polls :5000 every 5 ms, guest.transport.IDLE_POLL_S). It keeps its
   in-memory state and does not exit. **The worker must stop accepting :5000
   connections when it receives `parked` (before acking it)**, or the guest
   reconnects at once.
4. The worker snapshots the VM (`{session}/vm/snapshot`).
5. Resume: the worker restores that snapshot; the guest's idle loop connects
   and the reconnect of section 4 runs (no new hello; `seq` and `cseq`
   continue). The worker sends its pending control messages, `ui_response`
   first.

### Question park

With `answer_wait_s > 0` a question deadline makes the guest park
**itself**: no `park` control is involved. The worker sees `parked{reason:
"question", question_id}`, stops accepting :5000 as for any park, and
snapshots. The resume is the usual reconnect (or `hello{resume: true}` + the
log return for a fresh process, section 6); the worker then sends
`ui_response{question_id}` or `abort{reason}`. A parked-on question has no
deadline timer after the resume (the `answer_wait_s` wait is the host's: the
workflow's timer, or the direct driver's). If another question of the same
round passed its deadline before the park, answering the first parks again
on that one. With `answer_wait_s == 0` an unanswered question is denied
(three denials → `escalated`). Input-required mcp questions follow the same
rule.

## 6. Guest or VM death

1. The worker detects the death mid-task (not after `parked`).
2. It restores the base snapshot (fallback: a rootfs start) on the same
   `workspace.img` and sends `hello{resume: true}`. `max_resumes`
   (`HARNESS_MAX_RESUMES`, default 1) per task; then the task fails.
3. A fresh guest sends no handshake (it has no session yet); its first line is
   the ack of the resume hello's cseq. It takes that cseq as its floor:
   controls numbered below it are never delivered to it (the host treats them
   as delivered and re-sends anything that still matters, e.g. a task not yet
   in the log, with new cseqs after the hello). An `ack_seq` the host sends
   before the guest has fetched the log is ignored (logged).
4. The guest fetches the acked log with one bulk `get{kind: log, after_seq:
   0}` on :5001 (section 8), folds it, and continues from the returned
   `last_seq + 1`. It never writes a second `session_start`.
5. Every call open in the folded state (every side) is recorded
   `op_state{lost}` with the result `error: operation lost (VM restarted)`;
   the model is woken with those results and the task continues.

## 7. Gateway channel (:5011)

HTTP/1.1 from the guest to the host's gateway forwarder
(`agent-api/app/host/gateway_forwarder.py`), which proxies only `POST /mcp`
to the MCP gateway (docs/GATEWAY.md) and adds identity and the bearer. One
connection per request.

| Header / field | Value |
|---|---|
| request line | `POST /mcp` |
| `Content-Type` | `application/json` |
| `Accept` | `application/json, text/event-stream` |
| `X-SB-Call` | the event-log `call_id` (every `tools/call`) |
| `X-SB-Reattach` | `1` on a re-attach after a park only |
| body | `{"jsonrpc": "2.0", "id": "<call_id>#<attempt>", "method": "tools/call", "params": {name, arguments[, _meta: {"sb/input_response": {request_state, action, content?}}]}}` |

Answer: 200 with one JSON-RPC response (a JSON body, or an SSE body whose
event carries it); `result` is an MCP `CallToolResult` (`content`, `isError`,
optional `_meta["sb/input_required"]`). 410 on a re-attach: the forwarder no
longer holds the call → the guest records it `lost`. No `initialize`, no
`Mcp-Session-Id`. What the guest records: docs/GUEST.md "MCP over :5011".

### Identity headers

The forwarder passes the guest's headers on except hop-by-hop ones, `Host`,
`Content-Length`, the guest's own `Authorization` and every `X-SB-*` header
but `X-SB-Call` (`X-SB-Reattach` is never forwarded). It then sets
`X-SB-User`, `X-SB-Tenant`, `X-SB-Session` (the event-log `session_id`) and
`Authorization: Bearer $INTERNAL_TOKEN`. The gateway trusts the identity
headers only on a request that carries that bearer, so a peer on the compose
network cannot claim a tenant; because the forwarder adds the bearer, any
method or path other than `POST /mcp` is a 404 that never leaves the host.

### Forwarder-held calls

A snapshot resets the guest's vsock connections, so an MCP call cannot stay
open in the guest across a park. The forwarder owns the call instead:

- Every request runs in its own thread, independent of the guest's
  connection; its answer is kept in a per-session table `{call_id: running |
  done(status, body)}` keyed by `X-SB-Call`. The table outlives the
  forwarder's listener: closing :5011 around a park never drops a held call.
- If the guest's connection drops (park, snapshot, link loss), the gateway
  request keeps running and its answer is stored.
- The same `call_id` again (with or without `X-SB-Reattach: 1`) joins the
  running call or gets the stored answer at once; the gateway sees one
  request. An entry is dropped once its answer was written to a guest, so the
  `sb/input_response` re-send (same `X-SB-Call`, no `X-SB-Reattach`) is a new
  request.
- An unknown `call_id` with `X-SB-Reattach: 1` (agent-api restarted, the
  table is gone) → 410; the guest records the op `lost`. A call is **never
  re-executed**.
- The worker discards the entry of a call the guest declared `lost` (a death
  resume does not re-attach).
- A request without `X-SB-Call` gets an internal key no guest can name: it
  is never re-attachable.
- A gateway that cannot be reached is a 502 and a `gateway{op: error,
  source: forwarder}` host event.

## 8. Bulk channel (:5001)

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
`after_seq: 0`. The payload is `protocol.bulk.encode_log(events)` (canonical
JSON, one event per line). The guest checks a `get` payload's byte count and
`protocol.bulk.decode_log` its sha256 and that its seqs run gapless from
`after_seq + 1` to `last_seq` (an empty log must answer `last_seq` =
`after_seq`). A host that refuses a `put` header may reply
and close without reading the payload; the guest reads the reply even when
its payload write fails.
