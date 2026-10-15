# Guest harness — module contracts and threading model (M1)

The guest is one Python 3.12 process (`guest.main`), standard library only.
It never listens: it connects out to CID 2 (or, in tests, to Unix sockets
named `{dir}/v.sock_{port}`, the same names Firecracker gives the host side of
guest-initiated vsock connections). Ports (`protocol.ports.PORTS`): 5000 control, 5001 bulk, 5010 LLM, 5011 gateway.

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
- **The log writer** drains its queue in batches: one `EventLog.extend(batch)`
  (one fsync per batch), then `Transport.send_event` per event in seq order.
  Durable = host ack (`Transport` reports acks as `("ack", seq)` items; the
  log writer keeps the highest in memory and writes the `acked` marker at most
  every `ACK_MARKER_INTERVAL_MS` (200 ms) and on `flush()` / `stop()`).
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

`main`: parse args → (idle-wait: the snapshot point) → `Transport.start()`
on :5000 → wait for `hello` → `GuestConfig.from_hello` → `Coordinator.run()`
→ on `settle` flush the log, wait for the final ack (bounded), exit 0.
Fatal protocol errors (`second hello`, unsupported schema): emit
`error{fatal}`, exit 2.

## Module contracts (public names other modules import)

- `guest.config`: `GuestConfig` (dataclass from `hello.config`), `from_hello(body) -> GuestConfig`,
  `Principal(user, tenant)`, `Limits` (every default is `protocol.limits.DEFAULT_LIMITS`,
  the dict the host's `Worker.limits()` starts from too). A `hooks` key in an
  older host's hello is ignored (the hooks are code: `guest.hooks`).
- `guest.transport`: `Connector` protocol with `connect(port) -> socket.socket`;
  `UnixConnector(dir)`, `VsockConnector(cid=2)`; `Transport(connector, inbound,
  session_id=None)`: `start()`, `stop()`, `send_event(event)`, `send_live(type, body)`,
  `send_ack(cseq)`, `wait_acked(seq, timeout)`. Reconnect: backoff 0.1 s → 5 s,
  handshake = ack of the highest contiguous cseq, resend from ack+1
  (`protocol.messages.Outbox/Inbox/resend_plan`).
- `guest.eventlog`: `EventLog(dir)`: `append(event)`, `mark_acked(seq)`,
  `last_seq`, `acked_seq`, `read(after_seq=0)`, `rotate()`; `LogWriter(eventlog,
  transport)`: `start()`, `put(event)`, `flush(timeout)`, `mark_acked(seq)`
  (coalesced), `sync_acked()`, `stop()`. Segment names, the
  segment reader and `protocol.limits.SEGMENT_EVENTS` are protocol.replay's /
  protocol.limits' (the host store uses the same); the `acked` marker is written
  with `protocol.fsio.atomic_write`.
- `guest.context`: `Request` dataclass (`messages`, `tools`, `params`, `system_hash`,
  `request_hash`, `preamble_hash`, `chars`); `build_request(state, cfg, clock_at_ms) -> Request`;
  `system_block(cfg) -> str`.
- `guest.provider`: `Provider(connector, port=protocol.ports.LLM, model, timeout_s, *, backoff_s)`
  (guest.main passes `HARNESS_LLM_BACKOFF_S`, comma-separated seconds, when set: the tests
  set it small; usage sums go through `protocol.usage.normalise_usage`):
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
  provider, ops, clock)`: `run() -> settle body`; `emit(type, body) -> event`;
  `restore(events)`; `view` (the `TaskView`). Rules: "Coordinator" below;
  async mode below ("Async mode").

## Sides

A `tool_call` whose side is `host` or `device` (not `mcp`: see "MCP over :5011") is emitted and then
**waits for `side_result`** from the host (control message
`{call_id, side, content, ok}`); the coordinator turns it into `op_state` +
`tool_result`. Timeouts from `cfg.op_timeout_for(name, side)` (the hello
entry's `timeout_s`, else `cfg.limits.op_timeout_s[side]`).
Guest-side tools go to `OperationManager.submit`.

## Coordinator

**Modules.** `guest.coordinator` holds the loop (`run`, `_dispatch`), turns,
the provider call and tool calls, and re-exports the public names
(`Coordinator`, `SystemClock`, `FatalProtocolError`, `LOST_CONTENT`,
`INPUT_KINDS`, `HARNESS_VERSION`, ...). The rest are mixins of `Coordinator`:
`guest.questions` (questions, deadlines, the question park), `guest.mcp_ops`
(the mcp op lifecycle: "MCP over :5011"), `guest.park` (park, resume, the
settle and its manifest), `guest.restore` (resuming from a returned log).

**One reducer.** `guest.taskview.TaskView.apply(state, event)` owns every
piece of task bookkeeping that follows from the log: the open task
(`task_input_id`, its usage and ops baselines, regular-turn count, user-scope
memory slugs), the attachments and wall clock each task message named, the
round (`pending_inputs`, `unseen`, `turn_calls`, `turn_pending`,
`last_response_had_calls`, the turn without a response), side calls waiting
for `side_result`, open questions (kind `question` / `ask` / `mcp_input`,
`expired` once a `clock{question_deadline}` names them), doom-loop counts,
the park accounting (`wall_used_ms`, `live_since`, the last park's question
and `question_left_ms`) and the files of the last settle manifest.
`emit()` applies every event it writes to it, right after the fold;
`restore()` applies every returned event the same way, so a restored
TaskView is the one the live loop had (the suite checks this for every
coordinator scenario: `tests/conftest.py` `verify_task_views`). The loop's
handlers never edit it. What stays on the coordinator is what the log does
not say: timers, the mcp ops in flight (`_mcp_calls`), the provider call in
flight (`current_turn`), the questions past their deadline it parks on
(`_waiting`: a zero-timeout question without UI expires without a clock
event), the park request, the usage correction, the preamble registry (on
restore rebuilt per regular turn from the state before it) and the files
uploaded this process (`_uploaded`, seeded from the last settle manifest on
restore). None of it is hashed: checkpoints cover `protocol.fold.State` only.

**Sync wake rules** (`async_tools` false, the default): a turn starts when
nothing is running and nothing is awaited.
- The task's first turn starts on the task input (no wake event).
- After a response with tool calls, the next turn starts on the last
  terminal tool_result of the round: `wake{tool_result}`.
- After a response without tool calls, staged inputs (a follow_up or steer
  that landed mid-turn) start another turn: `wake{input}`; otherwise settle.
- Grace: armed after a response with tool calls; if it fires while
  operations still run it is recorded (`clock{grace_expiry}` + a log line)
  and has no other effect.

**Limits** (`cfg.limits`): the task's wall clock → `settle{timeout}`;
`max_tools` → one final turn without tools, then `settle{done}`;
`max_turns` → `error{limit}` + `settle{failed}`; `finish_reason` "length" →
no tool runs, `settle{done}` with the partial text; question deadline →
`guard_decision{deny, unanswered}` (three → `settle{escalated}`) or, with
`answer_wait_s > 0`, a question park; doom loop (same name + args three
times after the hooks) → an "ask" question. Time: every timer that has an
effect is first recorded as a `clock` event; `ts` is never read back; ids
are `derive_id(user, tenant, session_id, seq, kind)`.

**Known gaps**: an op timeout has no clock kind in the schema (recorded as
`op_state{timeout, duration_ms}` alone); malformed tool arguments get an
error result straight away (no repair turn).

## As built in M1 (deviations from the contract above, kept on purpose)

- `TOOL_DEFINITIONS[name]` is the full OpenAI tool entry (`{"type": "function",
  "function": {...}}`); `ToolSpec.schema` holds the bare parameters object.
- The transport acks every delivered control message itself; the coordinator
  only tracks `last_cseq` and never acks.
- The coordinator classifies inputs itself (`new` / `duplicate` on
  `State.seen_input_ids`; a `ui_response` for an unknown or answered question
  is `late`). The separate `guest.inbox` module is gone; its extra rule (a
  `ui_response` past the question's deadline by the fold's clock is `late`)
  was never used by the coordinator and is dropped with it.
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
  `turn_start.workspace_tree_id = ops.workspace_tree_id()` (cached: the
  workspace is re-walked only after an operation that may write finished —
  bash, write, edit, apply_patch; never after `guest.tools.READ_ONLY` tools
  or mcp ops).
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

## Park, resume, bulk, manifest

The wire side is docs/PROTOCOL.md sections 5, 6 and 8. What the guest does:

- **Snapshot point.** The base snapshot is taken with the guest running in
  idle-wait, before it has connected: at build time no host listener exists,
  so every attempt fails; after a restore the session's listener exists and
  the first attempt succeeds. `/workspace` is not mounted in the snapshot
  (`harness-init` leaves `/dev/vdb` alone, docs/IMAGE.md); the guest mounts it
  after idle-wait ends. A cold rootfs boot takes the same path. Nothing
  random is drawn before the snapshot point: every id is derived
  (`canonical.derive_id`) from what hello brings (session_id), so two VMs
  restored from one snapshot write distinct ids and logs.
- **Flags** (`guest.main`): `--idle-wait vsock` (the snapshot point: the
  transport tries :5000 every 5 ms (`IDLE_POLL_S`), silently, no backoff; before
  the transport starts, `preload()` imports every session module so the
  snapshot holds them and a restore imports nothing; idle-wait ends with
  the first line the host sends, so any connect error *or* a reset right after
  connecting, as a Firecracker snapshot causes, goes back to polling);
  main waits on `Transport.wait_awake()`; `--mount-workspace DEV` (after
  idle-wait, before hello is read: mount(2) through libc, ext4 + MS_NOATIME,
  no fork/exec; `mount -o noatime DEV WORKSPACE` when that fails; unless the
  workspace is already a mountpoint; a missing DEV is skipped; a failed mount
  is logged and the guest goes on). One lifecycle: the guest always stays up
  after a settle (`Coordinator.run()` handles one task per call) and returns
  to idle-wait after a park; it ends on an abort, a fatal error or when the
  host stops it.
- **Limits**: `park_grace_s` (default 30) joins `hello.config.limits`.
- **Wall clock per task**: a `task` / `follow_up` / `steer` message may carry
  `wall_clock_s` (the host always sends the task's: the tenant's value, else
  `HARNESS_WALL_CLOCK_S`). The input event logs it; the task the input
  starts arms that clock (`Coordinator._task_wall_s`), parks pause it
  (`parked.wall_used_ms`) and a restore re-arms what is left of it from the
  logged value. `hello.config.limits.wall_clock_s` is only the default for a
  message without one. Expiry → `clock{wall_clock}` → `settle{timeout}`.
- **Park** (`Coordinator`): honoured when no provider call is in flight and no
  guest operation runs; host/device/mcp calls and open questions do not hold
  it. While a park waits, no turn starts. Guest ops still running after
  `park_grace_s` → `clock{grace_park}` → `ops.abort_all()` → `op_state{killed}`
  + error `tool_result` (+ `wake` when that ends the round). Then: every timer
  paused (remaining time kept), `checkpoint{through_seq = last seq,
  state_hash = fold.state_hash(state), prev_hash}` (first: `hash_value(the
  session_start event)`, then the previous checkpoint's `state_hash`),
  `parked{checkpoint_seq, reason}`, wait (≤ 60 s) for the host's ack of
  `parked`; the connection that carries that ack is closed and the link goes
  back to idle-wait by the transport's link thread itself
  (`Transport.park_on_ack`, armed before `parked` is sent; `Transport.park()`
  after the 60 s): the host snapshots right after its ack, and a guest frozen
  before a later close would, once restored, close the restored link (an ack
  that comes only with a reconnect's handshake parks nothing). The next
  `("link", "up")` resumes: `clock{resumed, at}` first (the guest clock at
  the wake; it ends the parked stretch on the log), timers re-armed with what
  they had left, a deferred turn starts. A second
  `park` while parking is ignored. LocalSandbox's SIGSTOP/SIGCONT works on the same
  path (the stopped process resumes polling and reconnects).
- **Death resume**: a fresh process gets `hello{resume: true}`; its transport
  takes the hello's cseq as its floor (`Inbox.resume_from(cseq - 1)`; controls
  that arrived before the hello with other cseqs are never delivered). main
  then does a bulk `get{log, after_seq: 0}` (3 attempts; failure → exit 2),
  moves any older local buffer to `events.before-resume-N`, writes the
  returned events as the new buffer (all marked acked), `Transport.seed_seq
  (last_seq)` (next event = last_seq + 1) and `Coordinator.restore(events)`:
  the log is folded into a fresh State and through the same `TaskView`
  reducer the live loop uses ("Coordinator"), so the open task, the round,
  side calls, questions, attachments, user memory and doom-loop counts are
  what the live loop had; each regular turn's preamble text is rebuilt and
  kept when its hash matches `turn_start.preamble_hash`. `run()` then emits
  `clock{host_hello, at}` (when hello.at > 0), `error{transient, ref: turn}`
  for a turn whose response never came, `op_state{lost}` + `tool_result
  {"error: operation lost (VM restarted)"}` per open call, and wakes:
  `wake{tool_result}` + the next turn when a round was open; the settle the
  dead guest owed when the last recorded response had no tool calls; a new
  turn otherwise. The wall clock gets what the task had left. No second
  `session_start`. A returned log whose last event is `parked` (a fresh
  process after the host stopped a parked guest) loses nothing: side calls and questions wait
  again with fresh timeouts, and `run()` emits `clock{resumed, at}` first
  (before `clock{host_hello}`): a wake from a park always writes it. The usage correction restarts at 1.0.
- **Bulk** (`guest/bulk.py`, :5001, one transfer per connection): `BulkClient
  (connector, session_id, port).put(kind, path, data, sha256=None)` /
  `.get_log(after_seq)`
  (checks bytes, sha256 and gapless seqs). Spill files are put by the
  operation manager's worker thread as `spill` `.spill/{call_id}.txt` before
  the result is posted; the `bulk` event and `tool_result.spill_ref` carry that
  relative path (the local copy stays in `.harness/spill/`, which also holds
  `todo.json`). The digest computed for the spill is passed to the put. An
  upload failure is logged; the event is still written.
- **Settle manifest**: `settle.manifest = guest.bulk.settle_manifest(...)`:
  regular files under the workspace minus `.spill/`, `.git/` (excluded and
  sorted by `protocol.bulk.manifest()`; the walk only skips descending into
  them) and the guest's own `.harness/` (top level), symlinks skipped; file
  digests are cached by (size, mtime_ns, ino) across settles, and each file is
  hashed once per settle (that digest goes in the put header); each file whose sha256 the
  host does not have yet (tracked per process, recovered from the last
  `settle.manifest` after a restore) and under 8 MiB is put as `workspace`,
  then the manifest as `manifest` `manifest.json` (canonical JSON: sorted keys,
  no spaces). Upload failures are logged; the settle still carries the manifest.

## MCP over :5011

The wire side is docs/PROTOCOL.md section 7 (hello entries: section 2); the
gateway is docs/GATEWAY.md. What the guest does:

- **Config**: `ports.gateway` (default 5011). Each `hello.config.tools` entry
  keeps its `schema` (OpenAI function def; a bare `description` stands in when
  there is none) as `GuestConfig.tool_schemas` and its `timeout_s` as
  `GuestConfig.tool_timeouts` (in `config_hash`), for every side.
  `session_start.tools` stays `{name, side, pin}`.
  `cfg.op_timeout_for(name, side)` = the entry's `timeout_s`, else
  `limits.op_timeout_s[side]`.
- **Client** (`guest/mcp.py`, stdlib): `McpClient(connector, port)` posts one
  JSON-RPC `tools/call` per request to `POST /mcp` over the connector socket
  (the provider's `ConnectorConnection`), `Content-Type: application/json`,
  `Accept: application/json, text/event-stream`, `X-SB-Call: <call_id>`, no
  `initialize`. The JSON-RPC id is `<call_id>#<attempt>` (attempt = the
  input-required rounds so far). A JSON body, or an SSE body carrying the
  response, is accepted. Text items are joined with `\n`; `isError` → an
  error result (`error: ` prefixed by ops, the one place the guest adds it,
  unless the text already starts with `error:`; the gateway relies on
  `isError` and adds no prefix); connection errors, non-200 statuses and
  JSON-RPC errors → `error: gateway ...`. Nothing is retried (a tools/call is
  not idempotent; the forwarder's re-attach is the only re-send).
- **Operation**: an mcp call is a guest operation (no `side_result`):
  `op_state{running, side_ref: "gateway:<upstream>"}` (upstream = the prefix of
  the promoted name or of `call_tool`'s target; `gateway` for the other meta
  tools) → `OperationManager.submit_mcp` (its own daemon thread, not the pool)
  → `("op", ...)` → `op_state` + `tool_result` (spill applies). Op timeout =
  `op_timeout_for(name, "mcp")`, armed as an `mcp_timeout` timer (not paused
  on a park); when it fires `ops.cancel` closes the connection and the call
  is `op_state{timeout}`.
- **Input required** (docs/GATEWAY.md "Input-required relay"): ops posts `("op_input", call_id, request)` and
  forgets the job (no thread, no connection: the op is suspended, its op timer
  cancelled and its remaining time kept). The coordinator emits
  `op_state{input_required, attempt, input_request: {request_state, schema}}`
  and `question{call_id, text: message, options, deadline}` (options = the
  values of a single-property enum schema, else free text; deadline =
  min(question_timeout_s, expires_in_s)). `ui_response` →
  `op_state{running, attempt + 1}` and the same `tools/call` with `_meta["sb/input_response"]`
  (`guest.mcp.input_response`: action `accept` with `content` mapped into the
  schema — one property → `{prop: answer}` coerced to its enum/type; several →
  a JSON-object answer as is, else the first required property; an answer of
  exactly `decline` / `cancel` → that action), the op timer re-armed with what
  it had left. Deadline → `clock{question_deadline}` + `guard_decision{deny,
  unanswered}` (counts toward escalation) + the re-send (`op_state{running}`)
  with action `cancel`; the gateway's answer is the result. Repeatable: each
  round is one more `input_required` / `running` pair before the one terminal
  state.
  Escalation, abort and wall clock send a best-effort cancel for suspended
  calls (`ops.send_mcp`).
- **Park** (docs/PROTOCOL.md "Forwarder-held calls"): neither a suspended nor an in-flight mcp op holds a park.
  Parking (and a park grace, before it kills guest ops) detaches in-flight mcp
  ops (`ops.detach_mcp`: connection closed, nothing reported). Their op timer
  keeps its absolute due time across the park. On the resume (`link up`) an
  expired one is `op_state{timeout}`; the others re-send the same request with
  `X-SB-Reattach: 1`; a 410 → `op_state{lost}` + `error: operation lost ...`
  (never re-executed). A fresh process (restore after a death or after a
  host-stopped park) never re-attaches: in-flight mcp calls are lost; a
  suspended one (rebuilt from the folded `op_state{input_required}`: its
  `attempt` and `input_request`) waits for its answer and re-sends.
- **Not done**: an mcp connection broken by a link loss without a park is an
  error result (no automatic re-attach).

## Questions park the task; escalation via abort

- **Limit**: `answer_wait_s` (int ≥ 0, default 0) joins `hello.config.limits`
  (part of `config_hash`). The worker sends the tenant's value on both
  paths (the direct driver handles a question park like the workflow); 0
  denies an unanswered question (three → escalated; an input-required op
  is cancelled).
- **Deadline with `answer_wait_s > 0`** (`Coordinator._question_expired`):
  `clock{question_deadline}`, the deadline timer is dropped, the question is
  kept open (`_waiting`), and a park with reason `question` is requested; it
  happens at the loop's next `_maybe_park` (never mid-round), through the
  usual park path (guest ops get `park_grace_s`, mcp in-flight ops detach, timers pause,
  `checkpoint` + `parked{..., question_id}`, ack wait, idle). The intent is
  one flag (`_question_park_pending`); `_park` parks on the oldest question in
  `_waiting`. A host `park` still pending (`_park_reason`) is folded into it,
  and parks as asked if the question is answered before the park happens. An
  input-required mcp op stays suspended.
- **Resume**: same process — timers resume, the parked-on question has none;
  fresh process — question deadlines come from the park (`restore`):
  `parked.question_left_ms` holds what each open question's deadline had
  left when its timer paused (0 for one already past it, in `_waiting`).
  Question deadlines pause while parked (as live: `question_deadline` is in
  `PAUSED_ON_PARK`), so the parked hours never count. `_resume_after_park`
  re-arms every listed question with what it had left (`_arm_question(qid,
  delay_s)`), and sends every question that is due (named by a logged
  `clock{question_deadline, ref}`, or 0 left — that one first gets its
  `clock{question_deadline, ref}`) through `_question_expired`, oldest
  first: with `answer_wait_s > 0` they all join `_waiting` (no new park when
  the log's park is on one of them — the host resumed it for that answer);
  with 0 they are denied. A question the park did not list had no deadline
  and is not re-armed (an mcp input request's `expires_in_s` is a deadline
  like any other, also with `question_timeout_s` 0 and UI). Every park during a
  task records `parked.wall_used_ms` (the wall limit minus what the paused
  wall timer had left); the restored wall clock is the last park's value plus
  the guest-clock time since its `clock{resumed}` (the only event that ends a
  parked stretch; never `host_hello`/`host_heartbeat`, which
  carry host time), never the parked hours. A `ui_response` then works as before; if another question is still
  past its deadline the task parks again on it (`_park_next_waiting`).
- **Abort** (`_abort`): reason `escalated` → `settle{escalated, answer: ""}`;
  any other → `settle{aborted, answer: ""}`; `settle.reason`
  stays `abort` (main exits 0 on it), `detail` = the reason.

## Async mode (`hello.config.async_tools: true`)

Off by default (PLAN.md D6); with it off every log is byte for byte what it
was. The measurement lives in docs/ASYNC-TOOLS.md.

- **System block.** A fixed `## Tool calls` section (`context.ASYNC_TOOLS_NOTE`)
  follows the base prompt: calls run asynchronously, a running call shows the
  result `[pending]`, its real result arrives later as a message starting
  `[late tool result] call_id: <id>`, and the model may continue with
  independent work or end its turn without tool calls to wait. It is part of
  the system text, so it is in `session_start.system_hash` (and
  `async_tools` in `config_hash`); it never changes within a session.
- **Wake rule** (`Coordinator._async_tick`). Calls run on across turns. The
  loop runs the tick only when the inbound queue is empty, so every item
  already queued (coincident results, an input) is handled first and lands
  in the same turn (the slurp). With no provider call in flight, the tick
  wakes the model when a result or an input is unseen:
  - after a response whose calls still run, `limits.grace_s` batches
    results: a result inside the window waits for it, unless no call runs any
    more (then `wake{tool_result}` at once, as in sync mode). The window
    closing with results unseen is `clock{grace_expiry}` + `wake{grace,
    result_ids}`; closing with none is only the clock event, and the first
    result after it wakes;
  - after the window, every terminal result wakes: `wake{tool_result}`;
  - a staged `follow_up`/`steer` wakes at once, grace or not: `wake{input}`;
  - `wake.result_ids` = the calls whose result landed since the last regular
    `turn_start`, in arrival order; `input_ids` = the inputs staged since.
  The model may answer with more tool calls while earlier ones run; they
  join the running set (`max_tools` counts every call of the task as before;
  once reached, each further wake is the no-tools wrap-up turn).
- **Settle.** A response without tool calls settles only when no call runs
  (open questions and held mcp calls count as running) and no result or input
  is unseen. Otherwise the coordinator waits: the next result wakes the model.
  `max_turns` reached with calls running kills them (`error: killed`
  results) before `settle{failed}`; `wall_clock`, `abort` and escalation
  close open calls as in sync mode. The idle timer is unchanged.
- **Context.** A turn started while calls are open shows each open call as
  `{"role": "tool", "tool_call_id": id, "content": "[pending]"}` at the
  call's position (`context.placeholder_items`: exactly the placeholders the
  fold commits at that `turn_start`, same order, so `request_hash` replays and
  the next request's prefix is this request verbatim). A result after its
  placeholder was committed is `tool_result{late: true}` (the fold's one late
  rule) and is shown in the next turn, after everything already shown, as a
  user message:

  ```
  {"role": "user", "content": "[late tool result] call_id: <id>, tool: <name>\n<content>"}
  ```

  A second `tool` message for an id the model already saw answered would not
  be well-formed chat (OpenAI-compatible servers may reject it, and the
  Qwen template does not render `tool_call_id`), and after a compaction the
  call it answers may be summarised away; the header names the call either
  way. The late message never takes the turn's preamble (that goes to the
  first new input).
- **Park, restore, questions, mcp.** Placeholders are fold state, so a
  restore keeps them; an open call lost on restore gets `late: true` when its
  placeholder was committed. A restored task with unseen results or inputs
  wakes through the tick; a lost turn (or the task's first) is re-run. A
  parked task resumes waiting for its open calls; their results are late.
  Questions and input-required mcp calls are open calls like any other
  (shown `[pending]` until answered). Compaction counts placeholders in the
  estimate (they are in the request) and may summarise them away; a late
  result after that still renders as the late message.
