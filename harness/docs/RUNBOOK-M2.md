# M2 runbook: context, compaction and streaming on the server

Follows `RUNBOOK-M1.md` (a VM boots, a session runs on `engine: harness`).
This one verifies the three M2 exit criteria of PLAN.md section 13 against
the vLLM 0.11 container (`secondbrain-llm`, `vllm/vllm-openai:v0.11.0`,
`--enable-auto-tool-choice --tool-call-parser=hermes --reasoning-parser=qwen3`).

What M2 changed on the host (`agent-api/app/host/`):

| Piece | Change |
|---|---|
| `worker.py` | hello carries `params.stream` (from `LLM_STREAM`, default true) and `limits.context_window_tokens` / `reserve_tokens` / `token_budget` (from `HARNESS_CONTEXT_WINDOW_TOKENS` 32768, `HARNESS_RESERVE_TOKENS` 4096, `HARNESS_TOKEN_BUDGET` 0); the proxy gets the session key and the budget when its constructor takes them |
| `stream.py` | live `delta{kind: content}` fragments are `token` events verbatim; `reasoning` and `tool_call` deltas are dropped; a turn that streamed is not re-emitted at `llm_response`, and vice versa |
| `state_sync.py` | a guest `memory{scope: session}` event derived from a compaction turn is projected to `{session}/memory/compaction-<seq>.md` through `MemoryStore`, hash-checked against the event; every `memory` event is traced |
| `sides.py`, `skills/remember` | `remember` returns `sha256` of the written file, `forget` returns `sha256: ""`; Sides logs a `memory` host event `{scope, op, slug, sha256, call_id}` |

The guest side (streamed provider, token estimate, compaction turn) is in
`docs/GUEST.md` "M2 contracts".

Every command below assumes the compose stack is up with the local LLM
profile (`docker compose --profile local-llm up -d`) and runs from the repo
root. `$API_SECRET_KEY` is agent-api's bearer; `$SESSION` names a session dir
`agent-api/data/sessions/{user}/{tenant}_{session_id}` (inside the container:
`/root/agent-api/data/sessions/...`).

## 1. Prefix-cache hits from turn 2

Why it should hold: the system block is fixed at `session_start` (no
`{current_time}`), the tool list is fixed per session (D16), the committed
prefix is reused verbatim and the per-turn preamble is the *last* user
message, so every request after the first shares its prefix with the one
before it. vLLM 0.11 runs the V1 engine with prefix caching on by default.

### 1a. The counters to scrape

vLLM's `/metrics` (port 8080 of the `llm` service, on the `llm_internal`
network only) exposes two **counters** per served model, in tokens:

| Metric (V1, vLLM >= 0.9.2, as exposed by Prometheus) | Meaning |
|---|---|
| `vllm:prefix_cache_queries_total{model_name="qwen3-14b"}` | prompt tokens looked up in the prefix cache (every scheduled request's prompt) |
| `vllm:prefix_cache_hits_total{model_name="qwen3-14b"}` | of those, tokens served from cached KV blocks |

The source names are `vllm:prefix_cache_queries` / `vllm:prefix_cache_hits`
(`vllm/v1/metrics/loggers.py`); the Prometheus client appends `_total`. The
older `vllm:gpu_prefix_cache_queries` / `vllm:gpu_prefix_cache_hits` names
are deprecated since 0.9.2 (removal planned for 0.12) and only appear with
`--show-hidden-metrics-for-version=0.9`; the V0 gauge
`vllm:gpu_prefix_cache_hit_rate` does not exist in V1. Grep for
`prefix_cache` and use whichever pair the build prints.

```sh
scrape() { docker compose exec -T agent-api curl -s http://secondbrain-llm:8080/metrics \
           | grep -E '^vllm:prefix_cache_(queries|hits)_total' ; }
scrape
# vllm:prefix_cache_queries_total{model_name="qwen3-14b"} 18342.0
# vllm:prefix_cache_hits_total{model_name="qwen3-14b"} 11776.0
```

### 1b. Hit rate over one session

Counters are cumulative across every request the server has ever seen, so
the rate for *one session* is the ratio of the deltas between two scrapes.
With `--max-num-seqs=4` and nobody else talking to the box the deltas are
the session's.

```sh
read Q0 H0 < <(scrape | awk '{printf "%s ", $2} END {print ""}')
# turn 1
curl -sN -X POST localhost:8000/chat -H "Authorization: Bearer $API_SECRET_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"session_id": "m2-cache", "message": "List the files in my workspace.", "input_id": "i-1"}' >/dev/null
read Q1 H1 < <(scrape | awk '{printf "%s ", $2} END {print ""}')
# turn 2 (same session, new input_id)
curl -sN -X POST localhost:8000/chat -H "Authorization: Bearer $API_SECRET_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"session_id": "m2-cache", "message": "And how big is the largest one?", "input_id": "i-2"}' >/dev/null
read Q2 H2 < <(scrape | awk '{printf "%s ", $2} END {print ""}')
python3 -c "q1,h1,q2,h2=$Q1-$Q0,$H1-$H0,$Q2-$Q1,$H2-$H1; print(f'turn1 {h1}/{q1}={h1/q1:.2f}  turn2 {h2}/{q2}={h2/q2:.2f}')"
```

Expected: turn 1 near 0 on a cold server (the system block + tool
definitions are new), turn 2 well above 0.5 - everything up to the previous
turn's last message is a hit; only the new preamble and user text are
misses. Within a task that runs tools, every turn after the first is a hit on
the whole prefix up to the new tool messages. Compare with the per-call
`prompt_tokens` in the session's `events/host-000001.jsonl` (`usage` host
events): the hit tokens of turn N should be about the prompt tokens of turn
N-1.

Per-request cross-check: vLLM reports `usage.prompt_tokens_details.cached_tokens`
in the chat-completions response when prefix caching is on; a direct
request repeated twice shows it:

```sh
docker compose exec -T agent-api sh -c 'for i in 1 2; do curl -s http://secondbrain-llm:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"qwen3-14b\",\"messages\":[{\"role\":\"system\",\"content\":\"$(head -c 4000 /dev/zero | tr \"\\0\" a)\"},{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":4}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[\"usage\"])"; done'
# second line: 'prompt_tokens_details': {'cached_tokens': N} with N close to prompt_tokens
```

If turn 2 shows no hits: (1) the system block differs between turns -
diff `turn_start.request_hash` is expected to change (the preamble does)
but the first `system` message in two consecutive proxy requests must be
byte-identical; capture them with `LLM_PROXY_DEBUG`-free means: the fold's
`committed_prefix` of the session (`state.to_canonical()["committed_prefix"]`)
is what was sent; (2) the tool list changed between turns - `session_start.tools`
is fixed, but a `max_tools` final turn is sent **without** tools and
therefore with a different chat-template prefix: that turn alone misses;
(3) the container was started with `--no-enable-prefix-caching`.

## 2. A 40-turn session compacts and replays

Compaction is a turn (GUEST.md): when the guest's estimate of the next
request (chars/4, corrected by the last `usage.prompt_tokens`) exceeds
`context_window_tokens - reserve_tokens`, it first runs a summary turn
(`turn_start{type: compaction}` -> `llm_response` -> `compaction{range,
summary_turn_id}` -> `memory{scope: session, op: write, slug: compaction-<seq>,
derived_from: summary_turn_id}`), and the fold rebuilds the committed prefix
from the summary. The host projects the summary to
`{session}/memory/compaction-<seq>.md` and warns in the agent-api log if the
event's sha256 differs from the summary text.

In M2 the guest still exits at settle and the host boots a fresh guest per
task (`HARNESS_STOP_AT_SETTLE=true`, one run `events/rNNNN/` per task), so a
prefix spans the turns of **one task**; a session-long prefix across tasks
is M3's resumable VM. "40 turns" is therefore 40 model turns inside one
task: raise `HARNESS_MAX_TURNS`, lower the window so the compaction fires
on a real but short session, and give the model a task that needs many tool
calls.

```sh
# on the agent-api service (compose environment or `docker compose run -e ...`)
HARNESS_MAX_TURNS=40 HARNESS_CONTEXT_WINDOW_TOKENS=8192 HARNESS_RESERVE_TOKENS=4096 \
  # compaction fires when the estimate passes 4096 tokens
curl -sN -X POST localhost:8000/chat -H "Authorization: Bearer $API_SECRET_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"session_id": "m2-compact", "input_id": "c-1",
          "message": "Create files n01.txt ... n30.txt in the workspace, one bash call per file, each containing its own number, then cat them all and tell me the sum."}'
```

What to look at (`S=agent-api/data/sessions/<user>/<tenant>_m2-compact`):

```sh
ls $S/memory/                       # compaction-<seq>.md (+ MEMORY.md index)
grep -h '"type":"compaction"' $S/events/r0001/*.jsonl | python3 -m json.tool | head
grep -h '"type":"memory"' $S/events/r0001/*.jsonl
grep -h '"type":"turn_start"' $S/events/r0001/*.jsonl | grep -c '"type":"compaction"'
grep -h '"event": "memory"' $S/logs/trace.jsonl
docker compose logs agent-api | grep -i 'memory .* sha256'   # must be empty: hash matched
```

Replay: `protocol.fold.replay(events)` validates the log, verifies the
checkpoints and folds it once; an empty problem list and a state whose
`committed_prefix` starts with the summary message is the exit criterion.
There is no CLI module yet (`python3 -m protocol.replay` is M6); run the
function over the run's segments:

```sh
cd harness && python3 - "$S" <<'EOF'
import glob, json, sys
from protocol.fold import replay, state_hash
run = sorted(glob.glob(sys.argv[1] + "/events/r*"))[-1]
events = [json.loads(l) for p in sorted(glob.glob(run + "/0*.jsonl"))
          for l in open(p, encoding="utf-8") if l.strip()]
state, problems = replay(events)
print(len(events), "events;", "problems:", problems or "none")
print("turns:", len(state.turns), "compacted_through:", state.compacted_through)
print("session_memory:", state.session_memory)
print("prefix head:", state.committed_prefix[0]["content"][:120] if state.committed_prefix else None)
print("state_hash:", state_hash(state))
EOF
```

Expected: `problems: none`; `compacted_through` = the `to` of the last
`compaction.range`; `session_memory` holds `compaction-<seq>: <sha256>`;
`sha256sum` of the summary text equals that hash:

```sh
python3 -c "
import hashlib,re,sys; t=open(sys.argv[1],encoding='utf-8').read().split('\n---\n',1)[1].strip()
print(hashlib.sha256(t.encode()).hexdigest())" $S/memory/compaction-*.md
```

The same fold runs in `StateSync` at every accept and on load, so a run
that stored at all already folded; the script is the independent check
(`REPLAY.md` invariants 3 and 7). Run it against the three goldens in
`harness/tests/golden/` first if in doubt about the environment.

Failure modes: `settle{failed, reason: overflow}` after an
`error{class: overflow}` means the summary itself did not fit (window too
small for the system block + tools: raise `HARNESS_CONTEXT_WINDOW_TOKENS`);
two compaction turns back to back never happen (the guest refuses a second
one while the prefix is only the last summary); a `memory` event whose
`derived_from` the host cannot resolve is logged as a warning and not
projected (the log stays canonical).

## 3. Streamed hermes tool calls on vLLM 0.11

D17: `LLM_STREAM=true` (default) makes the guest post `stream: true,
stream_options: {include_usage: true}` through the proxy and assemble
`content`, `reasoning_content` and `tool_calls` from the SSE chunks (tool
calls by `index`, argument fragments concatenated). The risk in PLAN.md 16
is that vLLM's hermes parser emits argument fragments that do not
concatenate to valid JSON. Verify the raw stream first, then the guest.

### 3a. The raw stream from vLLM

```sh
docker compose exec -T agent-api curl -sN http://secondbrain-llm:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{
  "model": "qwen3-14b", "stream": true, "stream_options": {"include_usage": true},
  "messages": [{"role": "user", "content": "What is the weather in Paris right now? Use the tool."}],
  "tools": [{"type": "function", "function": {"name": "get_weather",
             "description": "Current weather for a city",
             "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                            "unit": {"type": "string", "enum": ["c", "f"]}},
                            "required": ["city"]}}}],
  "tool_choice": "auto", "max_tokens": 200,
  "chat_template_kwargs": {"enable_thinking": false}}'
```

A good stream looks like this (one `data:` line per chunk; the first
tool-call chunk carries `id`, `type` and `name`, the rest only argument
fragments under the same `index`):

```text
data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}],...}
data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"chatcmpl-tool-9f1c","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}],...}
data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\": \"Par"}}]},"finish_reason":null}],...}
data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"is\", \"unit\": \"c\"}"}}]},"finish_reason":null}],...}
data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],...}
data: {"choices":[],"usage":{"prompt_tokens":212,"completion_tokens":31,"total_tokens":243}}
data: [DONE]
```

Check the assembly mechanically: concatenate the `arguments` fragments per
`index` and parse them.

```sh
docker compose exec -T agent-api curl -sN http://secondbrain-llm:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -d @/tmp/req.json |   # the body above, saved to a file
python3 -c '
import json, sys
calls = {}
for line in sys.stdin:
    if not line.startswith("data: ") or line.strip() == "data: [DONE]": continue
    c = json.loads(line[6:])
    for ch in c.get("choices", []):
        for tc in (ch.get("delta") or {}).get("tool_calls") or []:
            e = calls.setdefault(tc["index"], {"name": None, "args": ""})
            f = tc.get("function") or {}
            if f.get("name"): e["name"] = f["name"]
            e["args"] += f.get("arguments") or ""
        if ch.get("finish_reason"): print("finish_reason:", ch["finish_reason"])
for i, e in calls.items():
    try: print(i, e["name"], json.loads(e["args"]))
    except ValueError as exc: print(i, e["name"], "MIS-ASSEMBLED:", repr(e["args"]), exc)
'
```

Repeat 10 times (`for i in $(seq 10)`); the hermes parser is only sound
if every run prints a parsed dict and `finish_reason: tool_calls`.

### 3b. The symptoms of mis-assembly

- `MIS-ASSEMBLED` above: duplicated or missing argument fragments (a
  fragment appears twice, the closing brace is absent, or the first chunk
  repeats the whole `<tool_call>` block as `content`).
- `finish_reason: stop` with the tool call in `content` as literal
  `<tool_call>{...}</tool_call>` text: the streaming parser did not engage;
  the non-streamed path parses the same output fine.
- a tool-call chunk without `id` or `name` on its first `index`
  appearance.
- in the guest log (`$S/vm/console.log` or the local sandbox's stderr):
  the coordinator's validate step logging malformed JSON arguments for a
  built-in, followed by the one repair turn (`tool_call` with an error
  `tool_result` "arguments are not valid JSON") and, if the second attempt
  fails too, `settle{failed}`; in `events/`, an `llm_response` whose
  `message.tool_calls[].function.arguments` does not `json.loads`.
- streamed `content` that interleaves with reasoning: with
  `--reasoning-parser=qwen3` and `enable_thinking: false` there must be no
  `reasoning_content` deltas at all; any are dropped by the host projector
  but indicate the template kwargs did not reach vLLM.

### 3c. The fallback

If 3a mis-assembles or 3b shows up in a real session, flip the engine to
one non-streamed call per turn and restart agent-api:

```yaml
# docker-compose.yml, service agent-api, environment:
      - LLM_STREAM=false
```

```sh
docker compose up -d agent-api
```

Effect: hello carries `params.stream: false`; the guest posts
`stream: false`; the client still receives the answer as `token` events,
word by word from `llm_response` (the legacy shape) instead of per
fragment, so the phone's TTS keeps working with a longer first-token
latency. The event log is identical either way (`llm_response` records the
assembled message verbatim), so replay and the goldens do not depend on the
flag. Record the outcome - verified streaming, or the flag plus the symptom
seen - in the M2 exit notes; this is the "or the fallback documented"
branch of the exit criterion.

## 4. M2 exit checklist (PLAN.md section 13)

- [ ] 1b: turn 2 of a session shows `prefix_cache_hits` growing by about turn 1's prompt tokens (hit rate > 0.5 on the turn-2 delta); `cached_tokens` non-zero on a repeated direct request.
- [ ] 2: a one-task session with `HARNESS_MAX_TURNS=40` and an 8192/4096 window produces `turn_start{type: compaction}`, `compaction`, `memory`; `memory/compaction-<seq>.md` exists; the hash matches; the replay script prints `problems: none`.
- [ ] 3a repeated 10 times with no `MIS-ASSEMBLED`, or `LLM_STREAM=false` set in compose with the symptom noted.
- [ ] `uv run pytest` green in `agent-api/` (host: streaming projection, compaction memory projection, remember sha256, e2e with the real guest) and in `harness/` (guest).

## 5. Config keys introduced in M2

| Key | Default | Read by |
|---|---|---|
| `LLM_STREAM` | `true` | worker -> `hello.config.params.stream` |
| `HARNESS_CONTEXT_WINDOW_TOKENS` | `32768` (match `--max-model-len`) | worker -> `limits.context_window_tokens` |
| `HARNESS_RESERVE_TOKENS` | `4096` | worker -> `limits.reserve_tokens` |
| `HARNESS_TOKEN_BUDGET` | `0` (none) | worker -> `limits.token_budget` and the proxy's per-session budget (402 when exceeded) |

Until `app/config.py` defines them the worker reads them with these
defaults (`app.host.worker.cfg`), so an unset key is never an import error.
