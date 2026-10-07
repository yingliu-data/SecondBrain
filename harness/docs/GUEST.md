# Guest harness — module contracts and threading model (M1)

The guest is one Python 3.12 process (`guest.main`), standard library only.
It never listens: it connects out to CID 2 (or, in tests, to Unix sockets
named `{dir}/v.sock_{port}`, the same names Firecracker gives the host side of
guest-initiated vsock connections). Ports: 5000 control, 5001 bulk, 5010 LLM.

## Threads and the one queue

```
transport reader ─┐
provider call    ─┤──▶ coordinator.inbound (queue.Queue) ──▶ coordinator thread (single writer)
operation worker ─┤                                               │ emit(type, body)
timers           ─┘                                               ▼
                                                     log writer thread ──▶ eventlog (disk) ──▶ transport writer (vsock)
```

- **Only the coordinator thread** creates events: `Coordinator.emit(type, body)`
  assigns `seq`, stamps `ts`, applies `protocol.fold.apply(state, event)`,
  puts the event on the log writer's queue, and returns it. Nothing else
  touches `State`.
- **The log writer** appends each event to the local segment (`EventLog.append`)
  and then hands it to `Transport.send_event`. Durable = host ack
  (`Transport` reports acks as `("ack", seq)` items; the log marks them).
- **Inbound items** (tuples; first element is the kind):
  - `("control", msg)` — a host→guest control message (`protocol.messages.CONTROL_TYPES`)
  - `("ack", seq)` — host acked guest events up to seq
  - `("link", "up" | "down")` — transport connected / lost
  - `("llm", turn_id, response_dict | Exception)` — provider finished a call
  - `("op", call_id, result)` — an operation finished; `result` = `{"ok": bool,
    "content": str, "state": "done|failed|timeout|killed", "duration_ms": int,
    "spill": {"path": str, "sha256": str, "bytes": int} | None}`
  - `("timer", kind, token)` — a timer fired (`grace_expiry`, `idle_timer`,
    `question_deadline`, `wall_clock`, `op_timeout`); stale tokens are ignored
- The coordinator **never blocks**: provider calls run in a thread that posts
  `("llm", ...)`; operations run in the operation manager's threads; timers are
  `threading.Timer`s that post `("timer", ...)`.

## Lifecycle

`main`: parse args → (idle-wait: in M3 the snapshot point) → `Transport.start()`
on :5000 → wait for `hello` → `GuestConfig.from_hello` → `Coordinator.run()`
→ on `settle` flush the log, wait for the final ack (bounded), exit 0.
Fatal protocol errors (`second hello`, unsupported schema): emit
`error{fatal}`, exit 2.

## Module contracts (public names other modules import)

- `guest.config`: `GuestConfig` (dataclass from `hello.config`), `from_hello(body) -> GuestConfig`,
  `Principal(user, tenant)`.
- `guest.transport`: `Connector` protocol with `connect(port) -> socket.socket`;
  `UnixConnector(dir)`, `VsockConnector(cid=2)`; `Transport(connector, inbound,
  session_id=None)`: `start()`, `stop()`, `send_event(event)`, `send_live(type, body)`,
  `send_ack(cseq)`, `wait_acked(seq, timeout)`. Reconnect: backoff 0.1 s → 5 s,
  handshake = ack of the highest contiguous cseq, resend from ack+1
  (`protocol.messages.Outbox/Inbox/resend_plan`).
- `guest.eventlog`: `EventLog(dir)`: `append(event)`, `mark_acked(seq)`,
  `last_seq`, `acked_seq`, `read(after_seq=0)`, `rotate()`; `LogWriter(eventlog,
  transport)`: `start()`, `put(event)`, `flush(timeout)`.
- `guest.inbox`: `Inbox(state)`: `classify(control_msg) -> ("new"|"duplicate"|"late", input_body)`.
- `guest.context`: `Request` dataclass (`messages`, `tools`, `params`, `system_hash`,
  `request_hash`, `preamble_hash`); `build_request(state, cfg, clock_at_ms) -> Request`;
  `system_block(cfg) -> str`.
- `guest.provider`: `Provider(connector, port=5010, model, timeout_s)`:
  `chat_completion(request: Request) -> dict` (OpenAI chat-completions response,
  `stream: false` in M1) via `http.client` over the connector's socket; raises
  `ProviderError(transient: bool, status, message)`.
- `guest.tools`: `TOOL_DEFINITIONS: dict[name -> OpenAI function schema]`,
  `validate_args(name, args) -> list[str]`, `ToolSpec` with `side`.
- `guest.ops`: `OperationManager(workspace, limits, inbound)`: `submit(call_id,
  name, args) -> None` (guest-side tools; posts `("op", ...)`), `abort_all()`,
  `running() -> set[call_id]`, `spill_threshold`, per-file queue for `edit`/`write`,
  `bash` in its own process group with `op_timeout_s`.
- `guest.hooks`: `before_tool(state, call) -> ("allow"|"deny"|"ask", reason)`,
  `after_tool(state, call, content) -> str | None` (rewrite); pass-through defaults.
- `guest.coordinator`: `Coordinator(cfg, state, inbound, log_writer, transport,
  provider, ops, clock)`: `run() -> settle body`; `emit(type, body) -> event`.
  Wake rules and limits: PLAN.md section 6. Sync mode only in M1.

## Sides

A `tool_call` whose side is `host`, `device` or `mcp` is emitted and then
**waits for `side_result`** from the host (control message
`{call_id, side, content, ok}`); the coordinator turns it into `op_state` +
`tool_result`. Timeouts per side from `cfg.limits.op_timeout_s[side]`.
Guest-side tools go to `OperationManager.submit`.

## As built in M1 (deviations from the contract above, kept on purpose)

- `TOOL_DEFINITIONS[name]` is the full OpenAI tool entry (`{"type": "function",
  "function": {...}}`); `ToolSpec.schema` holds the bare parameters object.
- The transport acks every delivered control message itself; the coordinator
  only tracks `last_cseq` and never acks.
- The coordinator dedupes inputs directly on `State.seen_input_ids`;
  `guest.inbox.Inbox.classify` implements the same rule and is the one to use
  when the two are unified.
- Host/device/mcp tool entries may carry a `schema` key (the OpenAI
  definition) in `hello.config.tools`; `config.from_hello` keeps name/side/pin
  for the allowlist and `context.tool_schemas` reads the schemas.
- An allowlist denial is a `guard_decision` only (no `tool_call` event, since a
  call to an unoffered tool is a fold error); the context builder inserts a
  deterministic `MISSING_TOOL_MESSAGE` tool message so the chat stays
  well-formed. Validation, hook and limit denials emit `tool_call` +
  `op_state{failed}` + an error `tool_result`.
- A per-side operation timeout is recorded as `op_state{timeout}` without a
  `clock` event; `op_timeout` is not yet a `clock.kind` (follow-up: add it to
  `protocol.events.CLOCK_KINDS` and emit it first).
- One task per guest process in M1: the guest exits after settle and the host
  boots a fresh sandbox per task, so the host stores one log per run under
  `events/rNNNN/`. M3's session-long VM collapses this to one run.
- On every new :5000 connection the host sends its ack and resends all pending
  control messages instead of waiting for the guest's handshake ack; the guest
  inbox drops the duplicates.
