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
  `request_hash`, `preamble_hash`, `chars`); `build_request(state, cfg, clock_at_ms) -> Request`;
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

## M2 contracts (streaming, compaction, budgets)

Hello config additions: `params.stream` (bool, default true; `LLM_STREAM=false`
on the host sets it false), `limits.context_window_tokens` (32768),
`limits.reserve_tokens` (4096). The token budget is the host proxy's alone
(`HARNESS_TOKEN_BUDGET`; it answers 402): it is not part of hello.

- `guest.provider.Provider.chat_completion(request, *, stream=False, on_delta=None) -> dict`
  always returns the assembled non-streamed shape
  `{"id", "model", "choices": [{"index", "message", "finish_reason"}], "usage"}`.
  With `stream=True` it posts `stream: true, stream_options: {include_usage: true}`,
  parses the SSE chunks itself (hand-rolled, stdlib), assembles `content`,
  `reasoning_content` and `tool_calls` (by index; argument fragments concatenated),
  and calls `on_delta({"kind": "content"|"reasoning"|"tool_call", "index": int,
  "text": str})` per fragment. With `stream=False` it calls `on_delta` once with
  `{"kind": "text", "index": 0, "text": <the whole content>}`. Usage comes from
  the final chunk. A stream that ends without `finish_reason` is a transient
  ProviderError. `ProviderError.budget` is set for a 402; a 400 whose message
  mentions context length / maximum context → `ProviderError.overflow = True`.
- Coordinator: every regular turn passes an `on_delta` that sends live
  `delta{session_id, turn_id, kind, index, text}` per fragment; `llm_response`
  is emitted once from the assembled response (verbatim).
  `turn_start.workspace_tree_id = ops.workspace_tree_id()`.
  Token estimate = ceil(`Request.chars`/4) × correction (`Request.chars` is the
  length of the canonical text whose sha256 is `request_hash`), correction =
  last `usage.prompt_tokens` / that turn's raw estimate (1.0 until the first usage).
  Trigger a compaction turn before a regular turn when estimate >
  `context_window_tokens − reserve_tokens`, and on `ProviderError.overflow`
  (retry the regular turn once after compacting; a second overflow →
  `error{overflow}` + `settle{failed, reason: overflow}`).
- Compaction turn: `clock{turn_clock}` → `turn_start{type: compaction,
  includes_through_seq, request_hash, preamble_hash}` whose request is the
  system block + the committed prefix + one user message with the compaction
  instruction (goal · progress · decisions · next steps · facts; no tools
  offered) → `llm_response` (the summary) → `compaction{range: [first prefix
  origin seq, includes_through_seq], summary_turn_id, tokens_before,
  tokens_after}` → `memory{scope: session, op: write, slug: "compaction-<turn
  seq>", sha256, derived_from: summary_turn_id}`.
  The fold rebuilds the prefix from the summary; the host projects the summary
  into `memory/compaction-<seq>.md`.
- Memory facts: a memory event's `sha256` is `sha256_hex(text.strip())` of the
  memory's body text (UTF-8), `""` for a remove; for a compaction summary the
  text is the summary. The memories a host-side tool changed (`remember`,
  `forget`) are named by the host: its `side_result` carries
  `memory: {scope, op: write|remove, slug, sha256}`, and after the `ok`
  `tool_result` the coordinator emits `memory{scope, op, slug, sha256,
  derived_from: call_id}` verbatim (a fact the event schema rejects is logged
  and dropped; a failed result emits none).
- Budget: 402 → `error{budget}` → `settle{failed, reason: budget}`, no retry.
- Hooks purity test: a hook that reads `time`, `os.environ`, `random` or
  `socket` during `before_tool`/`after_tool` fails `tests/test_guest_hooks.py`.

### M2 as built (refinements to the contracts above)

- Live `delta` bodies carry `index` as well (`{session_id, turn_id, kind, index,
  text}`) so the host can assemble tool-call fragments by index.
- Deltas belong to the provider: it de-dups across its own retries (a cut
  stream re-streams from the start; the caller's `on_delta` still sees each
  piece of text once), and the non-stream path sends its one `text` delta from
  the provider too. Every delta of a turn is sent before that turn's
  `llm_response`; the coordinator sends none of its own.
- A compaction turn's `includes_through_seq` is the previous turn's: it commits
  nothing, so the summary replaces exactly the committed prefix and staged
  items follow it. Compaction turns stream nothing live and do not count
  against `max_turns`. Both turn types start the same way
  (`Coordinator._begin_turn`: `clock{turn_clock}` → `turn_start`), and
  `tokens_after` is estimated on a copy of the state with the same
  `compaction` event folded in.
- User-scope memories are not in the fold's state (it only counts them): the
  coordinator keeps the slugs the task wrote and lists them with
  `state.session_memory` as the preamble's `[new memory]` lines.
- The host declares `HARNESS_CONTEXT_WINDOW_TOKENS`, `HARNESS_RESERVE_TOKENS`
  and `LLM_STREAM` in hello; `HARNESS_TOKEN_BUDGET` configures only the proxy,
  which answers 402 once a session's `total_tokens` reach the budget.

## M3 as built (park, resume, bulk, manifest)

Contracts: docs/M3-CONTRACTS.md. What the guest does, and the refinements:

- **Flags** (`guest.main`): `--idle-wait vsock` (the snapshot point: the
  transport tries :5000 every 50 ms, silently, no backoff; idle-wait ends with
  the first line the host sends, so any connect error *or* a reset right after
  connecting, as a Firecracker snapshot causes, goes back to polling);
  `--idle-wait FILE` (the M1 form, kept); `--mount-workspace DEV` (after
  idle-wait, before hello is read: `mount -o noatime DEV WORKSPACE` unless the
  workspace is already a mountpoint; a missing DEV is skipped; a failed mount
  is logged and the guest goes on); `--exit-on-park` (exit 4 once parked);
  `--multi-task` (stay up after a settle; `Coordinator.run()` handles one task
  per call). Without `--multi-task` the guest still exits after one task.
- **Limits**: `park_grace_s` (default 30) joins `hello.config.limits`.
- **Park** (`Coordinator`): honoured when no provider call is in flight and no
  guest operation runs; host/device/mcp calls and open questions do not hold
  it. While a park waits, no turn starts. Guest ops still running after
  `park_grace_s` → `clock{grace_park}` → `ops.abort_all()` → `op_state{killed}`
  + error `tool_result` (+ `wake` when that ends the round). Then: every timer
  paused (remaining time kept), `checkpoint{through_seq = last seq,
  state_hash = fold.state_hash(state), prev_hash}` (first: `hash_value(the
  session_start event)`, then the previous checkpoint's `state_hash`),
  `parked{checkpoint_seq, reason}`, wait (≤ 60 s) for the host's ack of
  `parked`, `Transport.park()` (close, back to idle-wait). The next
  `("link", "up")` resumes: timers re-armed with what they had left, a
  deferred turn starts (nothing is emitted for the resume itself). A second
  `park` while parking is ignored. With `--exit-on-park` the process exits 4
  instead of going idle. LocalSandbox's SIGSTOP/SIGCONT works on the same
  path (the stopped process resumes polling and reconnects).
- **Death resume**: a fresh process gets `hello{resume: true}`; its transport
  takes the hello's cseq as its floor (`Inbox.resume_from(cseq - 1)`; controls
  that arrived before the hello with other cseqs are never delivered). main
  then does a bulk `get{log, after_seq: 0}` (3 attempts; failure → exit 2),
  moves any older local buffer to `events.before-resume-N`, writes the
  returned events as the new buffer (all marked acked), `Transport.seed_seq
  (last_seq)` (next event = last_seq + 1) and `Coordinator.restore(events)`:
  the log is folded into a fresh State and the loop's bookkeeping rebuilt (the
  open task, the round, side calls, questions, attachments, user memory,
  doom-loop counts; each regular turn's preamble text is rebuilt and kept when
  its hash matches `turn_start.preamble_hash`). `run()` then emits
  `clock{host_hello, at}` (when hello.at > 0), `error{transient, ref: turn}`
  for a turn whose response never came, `op_state{lost}` + `tool_result
  {"error: operation lost (VM restarted)"}` per open call, and wakes:
  `wake{tool_result}` + the next turn when a round was open; the settle the
  dead guest owed when the last recorded response had no tool calls; a new
  turn otherwise. The wall clock gets what the task had left. No second
  `session_start`. A returned log whose last event is `parked` (a fresh
  process after `--exit-on-park`) loses nothing: side calls and questions wait
  again with fresh timeouts. The usage correction restarts at 1.0.
- **Bulk** (`guest/bulk.py`, :5001, one transfer per connection): `BulkClient
  (connector, session_id, port).put(kind, path, data)` / `.get_log(after_seq)`
  (checks bytes, sha256 and gapless seqs). Spill files are put by the
  operation manager's worker thread as `spill` `.spill/{call_id}.txt` before
  the result is posted; the `bulk` event and `tool_result.spill_ref` carry that
  relative path (the local copy stays in `.harness/spill/`). An upload failure
  is logged; the event is still written.
- **Settle manifest**: `settle.manifest = guest.bulk.settle_manifest(...)`:
  regular files under the workspace minus `.spill/`, `.git/` and the guest's
  own `.harness/` (top level), symlinks skipped; each file whose sha256 the
  host does not have yet (tracked per process, recovered from the last
  `settle.manifest` after a restore) and under 8 MiB is put as `workspace`,
  then the manifest as `manifest` `manifest.json` (canonical JSON: sorted keys,
  no spaces). Upload failures are logged; the settle still carries the manifest.
