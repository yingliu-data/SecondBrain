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
| `side_result` | `call_id`, `side` (`host`/`device`/`mcp`), `content`, `ok` |
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
{"type":"delta","body":{"turn_id":"turn_3f2a","kind":"text","text":"Here is"}}
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
4. Open host-originated calls are recorded `op_state{lost}`.
5. `max_resumes` is 1.
