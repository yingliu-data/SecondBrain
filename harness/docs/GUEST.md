# Guest harness — module contracts and threading model (M1)

The guest is one Python 3.12 process (`guest.main`), standard library only.
It never listens: it connects out to CID 2 (or, in tests, to Unix sockets
named `{dir}/v.sock_{port}`, the same names Firecracker gives the host side of
guest-initiated vsock connections). Ports: 5000 control, 5001 bulk, 5010 LLM, 5011 gateway (M4).

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

A `tool_call` whose side is `host` or `device` (M4: not `mcp`, see "M4") is emitted and then
**waits for `side_result`** from the host (control message
`{call_id, side, content, ok}`); the coordinator turns it into `op_state` +
`tool_result`. Timeouts from `cfg.op_timeout_for(name, side)` (the hello
entry's `timeout_s`, else `cfg.limits.op_timeout_s[side]`).
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
  main waits on `Transport.wait_awake()`; `--mount-workspace DEV` (after
  idle-wait, before hello is read: `mount -o noatime DEV WORKSPACE` unless the
  workspace is already a mountpoint; a missing DEV is skipped; a failed mount
  is logged and the guest goes on). One lifecycle: the guest always stays up
  after a settle (`Coordinator.run()` handles one task per call) and returns
  to idle-wait after a park; it ends on an abort, a fatal error or when the
  host stops it.
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
  `park` while parking is ignored. LocalSandbox's SIGSTOP/SIGCONT works on the same
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
  process after the host stopped a parked guest) loses nothing: side calls and questions wait
  again with fresh timeouts. The usage correction restarts at 1.0.
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

## M4 as built (MCP over :5011)

Contracts: docs/M4-CONTRACTS.md (sections 3.1-3.3 and 4). What the guest does:

- **Config**: `ports.gateway` (default 5011). Each `hello.config.tools` entry
  keeps its `schema` (OpenAI function def; a bare `description` stands in when
  there is none) as `GuestConfig.tool_schemas` and its `timeout_s` as
  `GuestConfig.tool_timeouts` (in `config_hash`). This also fixes M1-M3, where
  `effective_tools` dropped host/device schemas and the model saw
  empty-parameters tools. `session_start.tools` stays `{name, side, pin}`.
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
- **Input required** (3.2): ops posts `("op_input", call_id, request)` and
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
- **Park** (3.3): neither a suspended nor an in-flight mcp op holds a park.
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

## M5 as built (questions park the task, escalation via abort)

- **Limit**: `answer_wait_s` (int ≥ 0, default 0) joins `hello.config.limits`
  (part of `config_hash`). The worker sends the tenant's value only on the
  Temporal path; 0 keeps the M1–M4 behaviour (deny, three → escalated; an
  input-required op is cancelled).
- **Deadline with `answer_wait_s > 0`** (`Coordinator._question_expired`):
  `clock{question_deadline}`, the deadline timer is dropped, the question is
  kept open (`_waiting`), and a park with reason `question` is requested; it
  happens at the loop's next `_maybe_park` (never mid-round), through the M3
  path (guest ops get `park_grace_s`, mcp in-flight ops detach, timers pause,
  `checkpoint` + `parked{..., question_id}`, ack wait, idle). The intent is
  one flag (`_question_park_pending`); `_park` parks on the oldest question in
  `_waiting`. A host `park` still pending (`_park_reason`) is folded into it,
  and parks as asked if the question is answered before the park happens. An
  input-required mcp op stays suspended.
- **Resume**: same process — timers resume, the parked-on question has none;
  fresh process — question deadlines are log-derived (`restore`): each open
  question's due time is its `question.deadline` on a "live" guest-clock axis
  that leaves out every parked stretch (from `parked.at`, written on every
  park, to the first guest-side clock event after it; the log's last park is
  still in its stretch). Question deadlines pause while parked (as live:
  `question_deadline` is in `PAUSED_ON_PARK`), so the parked hours never
  count. `_resume_restored` then re-arms every question that is not yet due
  with the time it had left at the park, and sends every question that is
  due (named by a logged `clock{question_deadline, ref}`, or due at or
  before the park — that one first gets its `clock{question_deadline, ref}`)
  through `_question_expired`, oldest first: with `answer_wait_s > 0` they
  all join `_waiting` (no new park when the log's park is on one of them —
  the host resumed it for that answer); with 0 they are denied. A question
  with no deadline (`question_timeout_s` 0 with UI) is not re-armed; an
  mcp input request's `expires_in_s` is honoured through `deadline`, except
  with `question_timeout_s` 0 and UI, where it is treated as no deadline, as
  before. Every park during a
  task records `parked.wall_used_ms` (the wall limit minus what the paused
  wall timer had left); the restored wall clock is the last park's value plus
  the guest-clock time since the first guest-side clock event after it (not
  `host_hello`/`host_heartbeat`, which carry host time), never the parked
  hours. A `ui_response` then works as before; if another question is still
  past its deadline the task parks again on it (`_park_next_waiting`).
- **Abort** (`_abort`): reason `escalated` → `settle{escalated, answer: ""}`;
  any other → `settle{aborted, answer: ""}`; `settle.reason`
  stays `abort` (main exits 0 on it), `detail` = the reason.
