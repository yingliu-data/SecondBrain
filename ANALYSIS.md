# SecondBrain — Deep Analysis Report (Final)

**Date:** 2026-06-13
**Method:** Multi-agent deep analysis — 9 subsystem mappers (backend + iOS contract), 7 dimension finders (bugs, security, performance, reliability, architecture, docs-drift, testing/CI), a project-state agent, adversarial verification (every finding attacked by 1–2 independent verifiers instructed to refute it), and a completeness critic whose gaps were investigated in a supplemental pass that traced the pose-spatial-studio integration.
**Result:** 91 raw findings → 69 deduped → **51 confirmed, 1 contested, 3 refuted, 17 gap findings**, 14 low-severity unverified.

**TL;DR:** The core chat loop works, but three of nine skills (email, github_cli, gitlab_cli) **cannot function in the deployed container**, iOS calendar-event creation **always fails** on a date-format contract mismatch, answers are silently truncated by a 512-token cap with thinking enabled, nothing is actually streamed, there are zero tests behind a no-gate/no-rollback deploy, and **no backup exists for any persistent state**. The repo is **public**, which raises the practical severity of the two structural security issues: the Docker socket in the internet-facing container, and a guest endpoint whose Origin check is nullified by its own production proxy. The biggest stranded work (~3,800 lines including the only test suites) sits on `feat/multi-agent`.

---

## 1. Where to debug (prioritized)

### Broken features users hit today

1. **"Create a calendar event" never works** — `IndexApp/Tools/CalendarTool.swift:39` parses with `ISO8601DateFormatter()` (requires a timezone), but the backend schema ([agent-api/app/skills/calendar/skill.py:48](agent-api/app/skills/calendar/skill.py)) instructs the LLM to send timezone-less `2026-02-20T15:00:00`. **Fix:** iOS fallback `DateFormatter("yyyy-MM-dd'T'HH:mm:ss")` in the local timezone.
2. **Email, github_cli, gitlab_cli are dead in production** — [docker-compose.yml](docker-compose.yml) passes no `EMAIL_*` vars (no `env_file:`), so `EMAIL_ACCOUNTS` is always `[]`; `gh`/`glab` binaries are never installed into `python:3.12-slim`. **Fix:** `env_file: .env` + bake binaries into a real image (or disable the CLI skills by default).
3. **Answers silently truncated/emptied** — `finish_reason='length'` is never handled ([agent-api/app/agent/loop.py:50,109](agent-api/app/agent/loop.py)) while the main loop runs with `LLM_MAX_TOKENS=512` and Qwen3 *thinking enabled* (only the avatar planner disables it) — thinking tokens eat the budget and the visible answer is cut or empty. **Fix:** disable thinking via `chat_template_kwargs` for chat, raise the default cap, handle `length` explicitly.
4. **`None`-content crash kills the SSE stream** — `loop.py:109` uses `.get("content", "")` but vLLM sends `content: null` (key present → `None`); `None.split` raises mid-stream, no `done` event, `None` written to history. **Fix:** `.get("content") or ""` in `loop.py:109` and `guest.py:125`.
5. **A turn is lost on client disconnect** — [agent-api/app/routes/chat.py:28-32](agent-api/app/routes/chat.py) saves only after normal generator completion. **Fix:** `try/finally`. Related (low): concurrent requests on the same `session_id` clobber each other (read-modify-write, no per-session lock).
6. **iOS duplicates side effects on retry** — `IndexApp/Network/AssistantClient.swift:137-151` retries the whole chat POST even when device tools already executed → duplicate calendar events/reminders. Also **ChatView re-entrancy**: choice buttons and voice transcripts bypass the `isProcessing` guard and corrupt the streaming response (`ChatView.swift:24-26, 78-80`).
7. **`plan_movement` crashes the stream on a bare JSON array** — [agent-api/app/skills/avatar_control/skill.py:304-308](agent-api/app/skills/avatar_control/skill.py) guards only against `str`. **Fix:** validate `isinstance(parsed, dict)` in `_parse_plan`; wrap `skill.execute` in try/except in the registry.
8. **LLM auto-start fragility** — [agent-api/app/agent/llm.py](agent-api/app/agent/llm.py): 404 when the profile-gated container doesn't exist (post-`compose down`) permanently fails outside the retry loop; no lock against concurrent starters (stampede); `ConnectTimeout`/`ReadError`/mid-response death are neither retried nor trigger auto-start (catch `httpx.TransportError`).
9. **Email-skill bugs behind #2:** FTS5 `MATCH` crashes on `@`/`-`/`.` queries ([email/store.py:142-167](agent-api/app/skills/email/store.py)); `reply_to_email` case-sensitivity mismatch ([email/skill.py:227-250](agent-api/app/skills/email/skill.py)); IMAP sequence numbers used as stable UIDs → duplicates/dropped mail ([email/imap_client.py:121-157](agent-api/app/skills/email/imap_client.py)); `is_read` hardcoded `False` and never refreshed — the "important unread" digest permanently includes read mail.
10. **Small leaks:** `tool_results` dict entries leak on the timeout race (`loop.py:93-99` — add `tool_results.pop(tc_id, None)` to the `finally`); gh/glab subprocess never killed on timeout (orphan can still mutate remote state — `proc.kill()` in the handler).

**Refuted by verification (do not chase):** `finish_reason='stop'`-with-tool-calls drop (vLLM v0.11.0 sets `tool_calls` finish reason correctly); the tool-result "404 race" from yielding before event registration (window is microseconds vs. a multi-second device round-trip); fallback `tool_call_id` collisions (vLLM always supplies IDs).

---

## 2. Security (graded for a single-user personal server; **repo is PUBLIC**, which raises practical severity — IndexApp is private)

| Severity | Finding | Location |
|---|---|---|
| **HIGH** | Docker socket mounted into the internet-facing container — any RCE = host root. Only legit use is one POST in `llm.py:62`. | `docker-compose.yml:67` |
| **HIGH** | Guest per-IP rate limit is structurally **one global bucket**: all production traffic egresses from the pose-spatial-studio proxy's IP (and the tunnel without `--proxy-headers` has the same effect) — 3 guest sessions/hour for the entire internet; trivial demo-DoS, no real abuse limit. | `auth/guest.py:47,119` + `pose-spatial-studio/backend/routes/secondbrain_proxy.py:31` |
| MEDIUM | Guest Origin check is nullified **through a legitimate deployed path**: the proxy forwards the client-supplied Origin verbatim (`if origin := request.headers.get("Origin"): headers["Origin"] = origin`). It was already spoofable by curl; now it's bypassed via production infrastructure. | `secondbrain_proxy.py:26-27`, `routes/guest.py:36-38` |
| MEDIUM | **Empty `API_SECRET_KEY` silently accepted** — compose injects `""` when `.env` is missing, so `Bearer ` + HMAC-keyed-on-empty-string passes. Fail fast on empty/short keys. | `config.py:12` |
| MEDIUM | gh/glab allowlist includes `api`, `auth`, `secret` — `gh api -X DELETE /repos/...` / `gh auth token` are one prompt injection away. | `skills/github_cli/skill.py:9-15` |
| MEDIUM | Prompt-injection sanitizer is a 7-regex denylist that forwards flagged content anyway (truncates to 2000 chars *before* matching); nothing enforces the "confirm before destructive actions" rule. | `agent/sanitize.py:5-20` |
| MEDIUM | `security.log` is unbounded and unrotated — every unauthenticated failed request appends; internet-reachable disk-fill vector. No logrotate, no docker `logging:` limits. | `main.py:14` |
| MEDIUM | Guest output cap never enforced — `MAX_OUTPUT_TOKENS=200` imported but not passed; guests get full 512-token generations and can trigger the 6,000-token planner. | `routes/guest.py:72-75` |
| LOW | "4-layer auth" is effectively one secret: HMAC keyed with the Bearer token sent verbatim in every request; signs only `ts+body` (no method/path → cross-endpoint replay in ±300s); bearer compare not constant-time; non-UTF-8 body → 500. | `auth/middleware.py:13-34` |

**Verified clean:** `.env` is untracked with empty git history; no secrets in tracked files.
**Recommended:** docker-socket-proxy (container-start only) or drop auto-start; key guest rate limiting on `CF-Connecting-IP` and have the proxy pass the real client IP; pin the proxy's upstream Origin instead of forwarding; restrict gh/glab to read-only verbs; add a confirmation gate for outbound actions; `RotatingFileHandler` for security.log; finish **PLAN.md Task 5** (WAF + edge rate limiting) — the only Phase 1–5 task never completed.

---

## 3. What to continue working on (project state)

1. **Branch hygiene (5 min):** current branch `fix/planner-max-tokens-6k` is already squash-merged (PR #39); local `main` is 1 behind origin. Switch, pull, delete the ~22 verified-merged local branches; delete `feat/vLLM-Gwen` (superseded). **Keep only `feat/multi-agent`.**
2. **Revive `feat/multi-agent`** — ~3,800 unmerged lines (orchestrator/planner/worker, session tickets/manifests/IPC/SessionQueue, user-memory system) **plus the project's only 4 test files** (557-line `test_orchestrator.py` among them). It's what PROJECT_STRUCTURE.md's tickets/IPC sections actually describe. Predates the vLLM rewrite (#37): rebase, drop the committed `.idea/workspace.xml`, run its suites, open a PR. Until then, clean the on-disk remnants it left in the working tree (`agent-api/app/session/sess_template/`, `agent-api/tests/__pycache__/`, `agent-api/data/users/`).
3. **IndexApp `feat/TTS-local-build`** — in-flight on-device TTS migration (4 commits, no PR; last commit "fix: Local TTS bug", 2026-04-30). Verify on device, version the CHANGELOG entry, ship.
4. **IndexApp PR #6** (network retry/backoff, open since 2026-02-23) — SecondBrain CHANGELOG v0.11.0 already claims it shipped; merge or close. When merging, fix the duplicate-side-effect retry first (§1.6).
5. **PLAN.md Task 5** — WAF, rate limiting, OWASP rules + TC5.1–TC5.4.
6. **Backups (new — nothing exists):** all persistent state (conversations.db, emails.db, skills.json, security.log) lives in one bind mount on one disk, and plaintext IMAP/SMTP credentials sit in container env. Add a nightly tar/rsync of `~/data/SecondBrain/agent-api/data` off-box, and rotate `.env` secrets.
7. **Docs cleanup** (actively misleading): `CLAUDE.md` → nonexistent centralized docs dir (real docs: `.claude/skills/second-brain/`); PROJECT_STRUCTURE.md → removed Gemini/llama.cpp design + unmerged multi-agent session design presented as live; README → ElevenLabs TTS (actually on-device `AVSpeechSynthesizer`), SSE-Starlette (unused), "3 containers" (4); `server-debug/SKILL.md` → llama.cpp (actually vLLM); PLAN.md → llama.cpp/GGUF architecture, stale checkboxes; CHANGELOG version/date inversion. Also `SYSTEM_PROMPT` itself (config.py:21-23) tells the model its only tools are calendar/reminders/contacts/clipboard/web-search — omitting email, github_cli, gitlab_cli, avatar_control, plausibly degrading tool selection.
8. **On-device QA residuals:** background wake word + battery (<15%/8h, now against WhisperKit) and WiFi/cellular/VPN reliability.

---

## 4. What to optimize

1. **Real streaming (biggest UX win)** — `llm.py:96` hardcodes `"stream": False`; nothing reaches the user until full generation (≤120s; 300s+ during cold start). The production proxy's 120s read timeout (`secondbrain_proxy.py:18`) kills guest `plan_movement` (3 sequential generations) and any cold start mid-stream — and the frontend silently swallows it (no `error` event in the protocol, malformed-JSON catch-and-ignore in `secondBrainService.ts:66-68`). Stream vLLM deltas; emit SSE heartbeats during tool waits/cold start; add an `error` event to the protocol.
2. **`plan_movement` = three sequential LLM generations** on one GPU, planner forced to emit all 16 joints per keyframe though rest-merge fills defaults. Delta-only joints ≈ 5–10× fewer output tokens; drop or consume the discarded `duration_ms`.
3. **Persist tool results into history** — `loop.py` appends tool calls/results only to in-turn `messages`; next turn re-runs tools or hallucinates. Trim by tokens, not `history[-20:]`.
4. **Deduplicate the guest loop** — `guest.py:54-134` is a drifted copy of `loop.py` (the missing guest `max_tokens` is drift damage). Parameterize `run_agent_loop`.
5. **Deploy pipeline:** build a pinned image in CI instead of volume-injection + unpinned boot-time `pip install`; add `compileall` + pytest gate (a SyntaxError currently ships to prod); snapshot before `rm -rf` for rollback; deepen the health gate (one signed `/api/v1/chat` round-trip — `/health` returns **200 even when degraded**, so today's gate passes with the LLM down; also return 503 when degraded); cheap staging = second compose service on another port/tunnel hostname; pin `cloudflared`/`dcgm-exporter` images (currently `:latest`).
6. **Session/store mechanics:** blocking SQLite on the event loop (`asyncio.to_thread` or aiosqlite); history stored as one ever-growing JSON blob reparsed every turn (cap or one-row-per-message); `SessionStore.get()` writes on read; per-search `DDGS()` construction; registry falls back to the full 16-tool catalog (~2,500 tokens) on any non-matching query and rebuilds tool defs per request.
7. **Cleanup of dead config:** `SESSION_SECRET` (injected, never read), `LOG_LEVEL` (never read; logging hardcoded INFO), `RATE_LIMIT_PER_MINUTE` (never read), the advertised-but-unimplemented `redis` session backend, three generations of avatar motion code coexisting in `body.py` (`BODY_REGIONS`, `MOVEMENT_CYCLES`, `apply_relative_movement` — all dead), and `JOINT_LIMITS` that contradict shipped leg-raise poses (clamped `plan_movement` can't reach poses `set_pose` ships unclamped). Optional: an idle-stop counterpart to auto-start, since vLLM pins ~85% of VRAM 24/7.
8. **Make the app importable outside the container** — import-time `FileHandler("data/security.log")` and CWD-relative paths block any pytest/TestClient harness (`main.py:14`); make paths env-overridable, lazy-open the handler. This is the prerequisite for §5 step 5.

---

## 5. Suggested order

1. Branch hygiene + one-line crash/leak fixes (§1.4, 1.5, 1.7, 1.10) — an afternoon.
2. iOS calendar date fix + `env_file` wiring + thinking/`max_tokens` fix (§1.1–1.3) — restores advertised features and answer quality.
3. Security: docker-socket proxy, gh/glab allowlist trim, real client IP for guest limits + pinned proxy Origin, empty-secret fail-fast, log rotation (§2).
4. Real streaming + heartbeats + protocol `error` event (§4.1).
5. Importability fix → pytest harness (seeded from the multi-agent branch's tests) → CI gate + rollback snapshot + honest `/health` (§4.5, 4.8).
6. Backups cron (§3.6).
7. Rebase and land `feat/multi-agent`; docs cleanup to match reality (§3.2, 3.7).

---

## Appendix A — Verification summary

| Verdict | Count | Notes |
|---|---|---|
| Confirmed | 51 | incl. all critical/high with 2 adversarial lenses each |
| Gap findings (critic pass) | 17 | pose-spatial-studio integration, backups, observability, dead config, public-repo context |
| Contested | 1 | email IMAP per-message fetch perf — mechanics confirmed by both verifiers; moot until `EMAIL_ACCOUNTS` reaches the container |
| Refuted | 3 | `finish_reason='stop'` tool drop; tool-result registration race; `tool_call_id` collision |
| Unverified (low, below cap) | 14 | listed in Appendix B |

## Appendix B — Low-severity findings (unverified)

- Guest loop allows two tool rounds then reports failure after visibly executing them — `routes/guest.py:69-70, 132-134`
- `PATCH /api/v1/skills` accepts non-boolean `enabled` — string `"false"` enables the skill — `routes/skills.py:22-28`
- Uppercase skill keywords (`PR`, `MR`, `CI/CD`) never match the lowercased query — `skills/registry.py:102, 111`
- Unvalidated request bodies → 500s on chat/tool-result/guest — `routes/chat.py:23-25`
- Planner `duration_ms` prompted for but discarded; planner `loop` overrides the caller's argument — `avatar_control/skill.py:45-50, 328-341`
- Guest cleanup task has no exception guard — one error kills rate-limit pruning for the process lifetime — `auth/guest.py:90-103`
- CHANGELOG version/date inversion and contradictory context-window size — `CHANGELOG.md:6, 48, 60`
- Agent loop docstrings still describe the removed cloud/fallback provider — `agent/loop.py:26, 38`
- PROJECT_STRUCTURE.md avatar facts stale (v2.0 vs v4.0.0; pose/cycle counts) — `PROJECT_STRUCTURE.md:54-56, 154, 329`

## Appendix C — Test harness starting points

Prerequisite: §4.8 (importability). First pins, highest value first:

1. `verify()` auth — golden HMAC signatures, stale timestamp, tampered body, non-UTF-8 body (fails today), empty-secret rejection
2. Agent-loop dispatch with a FakeLLM — pins the `content:null` crash, `finish_reason='length'` handling, tool-result flow (fixtures: capture one real vLLM response JSON)
3. Guest session manager + IP rate limiter (time-injectable; pins the now-enforced `MAX_OUTPUT_TOKENS`)
4. Avatar `_parse_plan` (dict / bare-array / fenced / malformed)
5. Prompt-injection sanitizer (table-driven, incl. truncation-order behavior)
6. SessionStore round-trip + disconnect-save + concurrent-session semantics
7. FTS5 query escaping in email search
8. Skill registry keyword filtering (case sensitivity, zero-hit fallback)
