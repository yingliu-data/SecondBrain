# SecondBrain v2 — Work Plan

> Companion to [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md). Every task cites the design section it implements and the file it touches.
> Verified against the working tree at `feat/traceable-session-ids` (47e9c71), 17 Sept 2026.
> **Legend:** 🔴 correctness bug biting today · 🟠 security · 🟡 capability · ⚪ ergonomics · 🔒 breaking client contract (needs a coordinated IndexApp release)

## How to use this

- **Read "The case against this plan" first.** This document argues with itself on purpose: the strategic section below concludes that most of these phases should wait behind a two-day experiment (Phase W), and the plan has been restructured to say so. Treat what follows as a costed menu, not a sequence to execute top to bottom.
- **Phases are ordered by dependency, not by appeal.** Phase W and Phase 0 are small, unglamorous, and unconditional; everything after them is gated on evidence that does not exist yet.
- **One PR per task group**, not per phase. Each task table row is sized to be a single reviewable diff.
- **Definition of done** for every task: code + a test in `agent-api/tests/` + `uv run pytest` green + the doc line it invalidates updated.
- **Types are the deliverable too — but earned, not front-loaded.** `FinishReason` and `ToolOutcome` belong in P0 because they are string sentinels that have caused live bugs. The rest of the typed layer (Phase 0.5) waits until there is a second implementation to generalise from.
- **The CI gate already exists** (`.github/workflows/deploy_agent_api.yml` runs `uv sync --frozen && uv run --frozen pytest -q` before the GHCR build). Do not add a task that bypasses it.
- Tasks marked 🔒 change the request contract with IndexApp. Those need a version handshake, not a flag day — see the note at the end of Phase 0.

**Effort is in ideal days for one person who knows this codebase.** Multiply by your own honesty factor. Full plan: ~50–60 ideal days. Minimum viable path (end of this document): ~8.

---

## The case against this plan

I wrote this plan and the design it implements. Here is the argument against both, made as strongly as I can make it, because together they propose roughly **50–60 ideal days** of work — call it four to six months at a realistic part-time pace — against a system that currently runs, for a benefit that is mostly hypothetical. Ten objections. The last section concedes what survives them.

**1. Sixteen days of infrastructure precede the first new user-visible capability.**
P0 (2) + P0.5 (5–6) + P1 (5) + P2 (4) ≈ 16 ideal days before Phase 3 delivers the morning train push — the first thing in this plan you would actually notice as a user. And Phase 3's own note says *"this is the phase that proves the thesis."* So the thesis stays unproven until roughly day 20, with sixteen days already sunk into scaffolding built specifically to support it. That is backwards. A cron job, a hardcoded Darwin REST call and an APNs push is **two days** with no gateway, no contracts package and no hypervisor. If the push is useful, everything downstream is justified. If it isn't — if it turns out you glance at the departure board anyway, or the notification arrives at the wrong moment and you mute it — you have saved fifty days by finding out in week one. **Fix: Phase W below, which must run first.**

**2. Three of the four v1 capabilities already exist on the phone in your pocket.**
Design §1 lists train times, place search, weather and voice as co-equal. They are not. Apple Weather does weather. Apple Maps does places and routes, better, with no 10,000-call quota. National Rail's own app does departures. What none of them does is *arrive unasked at 07:45 knowing which train you take* — proactivity is the product, and voice is the interface. Place search in particular is a worse Google Maps reached through a 14B model with a monthly quota attached; it is in the plan because it was in the brief, not because it earns four days.

**3. The plan spends 10–12 days containing a threat it cannot reach, and zero days on two confirmed live ones.**
Design §6.2's own table marks **"Guest rate limit is one global bucket" (HIGH)** and **"Guest Origin check bypassed via the production proxy" (MEDIUM)** as *"not fixed by this design."* The word "guest" appears **zero times** in this work plan. Meanwhile Phase 4 spends 10–12 days on microVM isolation whose threat model is container escape *after* an attacker has already defeated Cloudflare Access and HMAC auth to get RCE inside `agent-api`. One of these is a confirmed, exploitable, internet-reachable defect today; the other is defence-in-depth against a breach that has not happened. The plan funds the second and ignores the first. **Fix: P0-10 and P0-11 below.**

**4. The tool budget is blown, and the fix is three phases away.**
*(Numbers corrected after measurement — the original estimate of ~20 was wrong, and how it was wrong turned out to matter more than the count. See "What measuring it actually found" below.)* A fresh registry exposes **16 tools**: `avatar_control` 4, `email` 3, `calendar` 2, `reminders` 2, and one each from `clipboard`, `contacts`, `web_search`, `github_cli`, `gitlab_cli`. Design §8.3 caps context at ten, citing Qwen3-14B's selection accuracy past that point. Phase 2 then adds Darwin, weather and maps on top. The fix — progressive disclosure — is **P6-6, in Phase 6**. So Phases 2 through 5 run with a tool surface the design itself says is past the degradation threshold, and neither document says so out loud. Either progressive disclosure moves ahead of Phase 2, or Phase 2's tools land only as others are removed. **Fix: P6-6 promoted to Phase 2 as P2-0.**

> **What measuring it actually found.** The estimate said `remember` contributed 3 tools. It contributes **zero** — `app/skills/remember/` was the only skill package missing an `__init__.py`, so `pkgutil.iter_modules()` skipped it in `_discover()` and the skill was never loaded. No error, no log line: the `try/except` around the import never runs, because the module is never reached. Meanwhile `data/skills.json` still carried a ghost `"remember": true` entry from before, which `_save_state()` round-trips forever.
>
> The consequence is bigger than the arithmetic. `ContextBuilder.build_system()` injects the memory index into the system prompt on **every turn**, and design §3.5 calls that memory system "the best-designed piece of the current tree" — but `remember`/`forget` are the only tools that write to it. **Durable user memory has been read-only in production.** An assistant premised on knowing you could not record anything it learned. That is the same class of failure as the `history[-20:]` window (gap #2), and it was invisible because nothing errored.
>
> Fixed: one empty `__init__.py`. Net tool count **16 → 13** after gating the dead skills and restoring `remember`.

**5. Three of those ten skills do not work in production, and no phase deletes them.**
ANALYSIS.md §1.2: `email`, `github_cli` and `gitlab_cli` **cannot function in the deployed container** — no `EMAIL_*` variables are passed by compose, and `gh`/`glab` binaries were never installed into `python:3.12-slim`. That is five dead tools consuming the budget in objection 4, plus `avatar_control`'s four, which serve a different product entirely (`robot.yingliu.site`) and have no business in a personal-assistant context window. Deleting or gating them is an hour of work that recovers half the tool budget, and it appears nowhere in sixty days of plan. **Fix: P0-12 below.**

**6. Phase 0.5 is premature abstraction, and the plan's own review proves it.**
Tactical finding #9 notes that `Harness.abort()` and `.steer()` are meaningless for `ScheduledHarness`. That is not a minor wart — **it is evidence that the interface is wrong, discovered before a single implementation of it exists.** You cannot design a good `Harness` ABC from one concrete case (`run_agent_loop`) and two imagined ones. The same applies to `ToolGateway`: the right shape will be obvious after the gateway is written, and retrofitting an ABC onto working code is an afternoon. Worse, the dependency graph puts P0.5 on the critical path, so a refactor delivering zero user value now gates the gateway, the tools and the deterministic tier. The genuinely valuable 20% — `FinishReason` and `ToolOutcome`, which replace string sentinels that have already caused one live bug — is two hours and belongs in P0. **Fix: P0.5 demoted off the critical path; its two load-bearing enums pulled into P0.**

**7. The latency budget the entire voice product rests on has never been measured.**
Design §8.1 presents a table of targets with no provenance. "vLLM, non-thinking, one tool round: 1,500 ms" is a guess. The three-second total — which the design calls *"the single most important number in the document"* — is the sum of seven guesses. If the real figure is 3,000 ms, then Phases 6 and 7 are built on fiction and the correct response is a smaller model or a narrower product, not a better harness. The measurement is an afternoon against a stack that already runs. **Fix: PW-2, a go/no-go gate on Phase 7.**

**8. Phase 4's rollback does not roll back.**
The stated fallback is `systemctl stop sb-*` then `docker compose up -d` at the previous tag. But P4-6 has already moved vLLM to a host systemd unit, so the old compose file's `LLM_URL=http://secondbrain-llm:8080` now resolves to a container that no longer exists, on a network (`llm_internal`) that no longer has a member. You do not return to the system you left; you begin a second migration under time pressure, at whatever hour the first one failed. **Fix: the rollback step is rewritten below.**

**9. Complexity moves in one direction only.**
The plan adds a contracts package, a second service, a hypervisor, a jail pool and five VMs. It deletes `_start_local_llm()`, `ssrf.py`, and possibly `_legacy_turn`. Design principle 2 is *"one orchestrator, thin clients"* — the endpoint of this plan is two orchestrating processes, N ephemeral jails and five VMs on one box, operated by one person. Neither document asks the operability question: when the 07:45 push does not fire and you are on a platform with a phone, how many layers must you reason through to find out why? P4-8's runbook is one row in a table; it is not an answer to that question.

**10. The plan migrates a working multi-tenant production system to build a personal feature.**
P1-10 rewrites the wcc tenant configuration. The wcc tenants — events, mentorship, analytics — are the only part of this system with users who are not you, and `timeout_s: 2700` on `wcc-events` says those pipelines are long-running and load-bearing. Nothing in the Jarvis brief requires multi-tenancy; the gateway touches it only because the gateway now owns tool policy. That is a real migration risk to a system with external users, taken on in service of a train reminder, and it is priced at zero in this plan.

### What survives the argument

Not everything here is wrong, and the objections above should not be read as "do none of it":

- **Phase 0's correctness bugs are real, cheap and independent of every strategic question.** The truncated-tool-call bug (P0-1) executes tool calls with `{}` arguments today. That is worth fixing whether or not anything else in this plan happens.
- **Compaction (P6-1) is a genuine product failure, not an ergonomic one.** `history[-20:]` means an assistant whose entire premise is remembering you forgets turn 1 at message 21. It is arguably the highest-value single item in the document and it sits in Phase 6.
- **The gateway (Phase 1) is correct design** *if* the tool surface grows past the handful it has today. It centralises credentials that are currently in the process terminating internet traffic. The argument above is about ordering, not merit.
- **Backups.** Nothing else in sixty days protects against the failure that actually destroys everything: the disk dies and every conversation, memory file and profile is gone. It is the ninth row of Phase 0 and should be hour one of day one.

The honest summary: **the correctness work and the backup are unconditional; compaction is the best single product fix; everything else should wait behind a two-day experiment that tells you whether the proactive thesis is true.**

---

## Tactical review — internal inconsistencies

Six findings changed the plan below; three are flagged as judgment calls. These are *internal* problems — places where the plan contradicted itself or the tree. The strategic argument against the plan as a whole is the section above.

### Fixed in this revision

1. **Phase 0.5 breaks the Docker build as written — verified, not hypothetical.** `.github/workflows/deploy_agent_api.yml` builds with `context: agent-api` (repo-relative), and `agent-api/Dockerfile` does `COPY pyproject.toml uv.lock ./` then `uv sync --frozen` from *inside* `agent-api/`. A `packages/sb-contracts/` workspace member living at the repo root is outside that build context — `uv sync` inside the container cannot see a sibling directory that was never sent to the daemon. This is not a style objection; the image would fail to build the moment `sb-contracts` becomes a real dependency. **Fix:** either (a) move the Docker build context to the repo root (`context: .`, `file: agent-api/Dockerfile`, `COPY packages/sb-contracts ./packages/sb-contracts` added before `uv sync`), or (b) skip the uv-workspace path entirely and vendor `sb_contracts/` as a subpackage of `app/` in each service, accepting the duplication PA-1 was trying to avoid. (a) is the one to build a task for, since it keeps the shared-types goal; new task **PA-0** below.
2. **Sanitize() has two contradictory destinations in the plan as written.** Phase 0's note says the sanitiser "gains a real home at the gateway in Phase 1" — but Phase 1's task table (P1-1…P1-11) has no such task, and Phase 6's P6-5 says `sanitize()` "moves" to `app/agent/hooks.py` in `agent-api` instead. Those can't both be where it lives. **Also missed:** device tools (calendar/reminders/contacts/clipboard) never touch the gateway — they round-trip over SSE to the phone (`loop.py` device-tool branch) — so whatever the gateway does to MCP results, `agent-api` still needs its own sanitisation for device-tool results. **Fix:** the gateway sanitises and size-caps everything that crosses it (design §4.2 responsibility 7, correctly a gateway job for MCP/server tools); `agent-api`'s `after_tool_result` hook (P6-5) stays, scoped specifically to device-tool results, which is the traffic the gateway structurally cannot see. Phase 0's note and P1's task table are corrected below to say this once, not twice, differently.
3. **PA-6 touches the IndexApp wire contract and isn't marked 🔒.** Replacing the nine hand-built `f"event: token\ndata: {json.dumps(...)}\n\n"` sites with `HarnessEvent.to_sse()` is exactly the kind of change the 🔒 legend exists for — a subtly different key order or an added field in the JSON is invisible in a Python diff and breaks IndexApp's SSE parser silently. A version-negotiated handshake (the P0-4 pattern) is the wrong tool here, though — this isn't a protocol version bump, it's a refactor that must produce byte-identical output. **Fix:** PA-6 gets a golden-file test (record real SSE output before the refactor, assert equality after) instead of a 🔒 tag; that's the correct guarantee for "same behavior, different code," not a client release coordination.
4. **The Phase 1 exit criterion cites a config variable that Phase 1 deletes.** "`agent-api`'s test suite passes with `MCP_ALLOWED_PRIVATE_HOSTS` empty" is P1's stated exit check, but P1-9 deletes `app/mcp/ssrf.py` — the only reader of that variable. Once P1-9 lands, the variable has nothing left to test. **Fix:** the SSRF check is an exit criterion for the *state before* P1-9, not after; reworded below to check it pre-cutover and drop the reference post-cutover.
5. **"Egress allowlist" in Phases 1–3 is application code, not a network boundary, and the plan doesn't say so.** `gateway.json`'s per-server allowlist (P2-4) is enforced by whatever HTTP client wrapper checks a destination host string — real network-layer egress restriction (a jail that structurally cannot reach anything off its allowlist) doesn't exist until P4-3/P4-7's microVM + jail-bridge work. Design §6.2 is honest about this gap ("two honest notes"); this plan wasn't. **Fix:** Phase 1 and Phase 2 exit criteria below now say explicitly that the allowlist is a policy check, not a network boundary, until Phase 4 ships — so nobody reads "Phase 1 done" as "credentials are contained" before they actually are.
6. **`DirectToolGateway` (Phase 0.5's in-process dev variant of `ToolGateway`) is a standing bypass of everything Phases 1–4 exist to build**, if it ever runs where the real gateway should. Design §4.2's whole argument is that policy enforcement must not be revocable by whoever holds `agent-api`'s process. **Fix:** new task PA-3b below — `DirectToolGateway.__init__` refuses to construct unless an explicit `SB_ALLOW_DIRECT_GATEWAY=1` env var is set, so it cannot become production configuration by omission.

### Flagged, not fixed — judgment calls

7. **Phase 0.5 is not derived from SYSTEM_DESIGN.md.** Every other phase traces to a design section; Phase 0.5 traces to a request made after the design doc was written, and the design doc's own principles (single-user, ~500 MB always-on footprint, "stay small") never call for a formal ABC/enum/dataclass layer. That doesn't make it wrong — the enum-for-string-sentinel argument (`ToolOutcome` replacing `.startswith("Error")`, `FinishReason` replacing the raw string at `loop.py:87`) is a real correctness improvement, not decoration — but it should be understood as an engineering-quality investment layered on top of the design, not a requirement flowing from it. Weigh the ~5–6 days (see #8) against Phases 1–3 actually shipping capability.
8. **Two effort estimates look optimistic against the plan's own description of the work.** Phase 0.5's "~3 days" covers 12 enums, 13 types, 5 ABCs, and 9 call-site migrations including a full SSE-event rewrite (PA-6) and a full LLM-payload rewrite (PA-8) — each plausibly a day or two alone once tests are included. Phase 4's "~5 days + a day of downtime risk" covers installing and learning Kata + Cloud Hypervisor for the first time (nothing in this tree touches VM tooling today — it's Docker Compose end to end), building the systemd units, the p2p virtio-net links, and a production cutover with a verified rollback — against design §5.3's own advice to "go slowly" on the hypervisor work. Revised to 5–6 and 10–12 days respectively below; treat both as the part of this plan most likely to slip.
9. **`Harness.abort()` / `.steer()` are abstract methods `ScheduledHarness` has nothing meaningful to implement.** A cron-fired train push has no session to abort mid-flight and no user to steer it. Originally logged here as a minor interface-segregation wart to accept with no-op methods — **on re-reading, it is the load-bearing evidence for objection 6 above.** An interface whose flaws are visible before any of its three implementations exist is an interface being designed too early. It is the reason Phase 0.5 is now demoted rather than merely annotated.

---

## Status — 17 Sept 2026

**Phase 0 is complete and Phase W's artefacts are written.** 132 tests pass (was 88 before this session; 12 test files, now 16). Nothing is committed — the working tree holds it all.

| Task | State | Note |
|---|---|---|
| P0-1 arguments fail closed | ✅ | **Brief was wrong about the mechanism** — see the corrected row below. The real bug was malformed argument JSON in a normal `tool_calls` message, not `finish_reason="length"`. Both paths now handled |
| P0-2 `tool_results` leak | ✅ | both dicts popped in the `finally` |
| P0-3 non-UTF-8 body → 500 | ✅ | HMAC over raw bytes |
| P0-4 HMAC covers method+path | ✅ | dual-accept on `X-Sig-Version`; v1 still works and logs `AUTH_WARN legacy_signature`. **IndexApp must ship v2 before v1 acceptance is removed** |
| P0-5 weak `API_SECRET_KEY` | ✅ | `require_strong_secret()`; CI key was 7 chars and would have broken, updated in the same change |
| P0-6 unbounded `security.log` | ✅ | `RotatingFileHandler`, 10 MB × 5 |
| P0-7 `MAX_TOOLS` split | ✅ | `MAX_TOOL_ROUNDS` + `MAX_TOOLS_IN_CONTEXT`; `max_tools` aliased for one release |
| P0-8 `_legacy_turn` disconnect | ✅ | mirrored, not deleted — `SESSION_BACKEND=sqlite\|memory` are live code paths |
| P0-9 backups | ➡️ PW-1 | script + systemd units written; **you must run it** |
| P0-10 guest rate limit (HIGH) | ✅ | `client_ip()` on `CF-Connecting-IP` → XFF → socket. **Proxy must forward the real IP** |
| P0-11 guest Origin | ⚠️ partial | server side documented + logged; **the real fix is proxy-side, in another repo** |
| P0-12 dead skills | ✅ | 16 → 13 tools. **Inert until you edit `data/skills.json` on the box** |

### Three things found by doing the work that the plan did not predict

1. **`remember` was never loaded.** `app/skills/remember/` was the only skill package with no `__init__.py`, so `pkgutil.iter_modules()` skipped it silently — no error, because the module is never reached. `ContextBuilder` injects the memory index every turn, but `remember`/`forget` are the only tools that write to it, so **durable user memory was read-only in production**. An assistant premised on knowing you could not record anything. Fixed with an empty file.
2. **The guest endpoint never disabled thinking.** Commit 58d66e3 turned thinking off in the agent loop *because thinking made Qwen3 describe tools instead of calling them* — `routes/guest.py` has its own loop and never got that fix. Every `avatar_control` request on the live robot demo has been running with thinking on. Fixed, along with the `content: null` crash (ANALYSIS.md §1.4) in the same file.
3. **`P0-1`'s premise was wrong** and only measurement caught it. Documented in the P0-1 row and in SYSTEM_DESIGN §2.3.

### What you have to do — none of this is reachable from the repo

- **PW-1** `scripts/backup.sh` — set `RESTIC_REPOSITORY`, install the timer, **run `backup.sh verify` once**. Highest-value item in the document and it is still not done until you run it.
- **PW-2** `scripts/measure_latency.py` — the go/no-go gate on Phase 7, and it also measures tool-selection accuracy at 6/10/20 tools, which tests the §8.3 ceiling that objection 4 rests on.
- **PW-3** `scripts/measure_gpu_idle.sh` — answers §11.4 and P4-6.
- **PW-4** `spike/train_push.py` — needs a Rail Data Marketplace key and an APNs `.p8`. **This is the one that prices the other 50 days.**
- **Deploy steps:** disable the three dead skills on the box (`data/skills.json` is gitignored and bind-mounted, so the code default does not reach it); have the pose-spatial-studio proxy forward `CF-Connecting-IP` and pin the upstream `Origin`.

---

## Phase W — Week one: protect, measure, disprove

**Goal:** three things that are unconditional, cheap, and change what the rest of this plan should be. Nothing here depends on any other phase, and objections 1, 3 and 7 above all say this runs first.
**Effort:** ~3 days. **Exit:** the data is backed up and restorable; the latency table has real numbers in it; you know whether a proactive push is useful to you.

| # | Where | Change |
|---|---|---|
| PW-1 | `scripts/backup.sh` + a systemd timer | **Hour one of day one.** Nightly `restic` or `rsync` of `data/sessions`, `data/users`, `data/skills.json`, `data/tenants.json` off-box, encrypted — **plus a restore you have actually performed.** An untested backup is not a backup. Nothing else in this plan protects against the one failure that destroys everything irreversibly (design §5.4). Was P0-9; promoted here |
| PW-2 | measurement, no code | **Fill in design §8.1 with real numbers.** Run 20 voice-shaped turns against the deployed stack and record: time to first token, time to `finish_reason`, one tool round end-to-end, and the same with `enable_thinking` both ways. Design calls three seconds "the single most important number in the document" and every figure behind it is a guess (objection 7). **This is a go/no-go gate on Phase 7** — if a non-thinking tool round is materially over ~1,500 ms, the voice product needs a smaller model or a narrower scope, and Phases 6–7 need rewriting before they are built |
| PW-3 | measurement, no code | Idle GPU draw, resident vs. unloaded, over an hour each. Answers design §11.4 and P4-6, costs nothing but wall-clock, and the answer changes the electricity line in §8.4 — the dominant running cost |
| PW-4 | `spike/` — **throwaway code, delete it after** | **The two-day experiment that prices the rest of this plan.** A cron entry, a hardcoded Rail Data Marketplace REST call for your actual commute, a template string, and an APNs push. No gateway, no MCP, no contracts package, no model. Run it for **one week**, then answer honestly: did the 07:45 notification change anything you did? If yes, Phases 1–3 are justified and you now have a working Darwin call to build P2-1 from. If no, stop — the remaining ~50 days are buying a worse Google Maps with a quota (objection 2), and the right plan is P0 + P6-1 and nothing else |

**Do not skip PW-4 because it feels like throwaway work.** It is throwaway work. That is the point: it costs two days to find out whether the other fifty are worth spending, and every phase after it is easier to justify — or easier to cancel — once it has run.

---

## Phase 0 — Correctness debt

**Goal:** stop the bleeding before building on top of it. Everything here is small, and all of it is currently wrong in production.
**Effort:** ~3 days (was 2 — P0-10/11/12 and the two enums pulled forward from Phase 0.5 are new). **Exit:** all tasks merged, `pytest` green, one deploy to the box with no regression in a manual smoke chat; the default tenant's tool count is under the design's ten-tool ceiling.

**Pulled forward from Phase 0.5** (objection 6): add `FinishReason` and `ToolOutcome` as `StrEnum`s in `app/agent/` — *not* a new package, just two enums next to the code that uses them. They are the load-bearing 20% of that phase and they make P0-1 and P0-2 correct by construction rather than by vigilance.

| # | Sev | Where | Change | Test |
|---|---|---|---|---|
| P0-1 | 🔴 | `app/agent/loop.py` | ✅ **Done — and the brief was wrong about the mechanism.** I claimed `finish_reason="length"` caused tools to run with `{}` arguments. It cannot: the tool branch requires `finish_reason == "tool_calls"`, so a truncated message fell through to the final-text path and produced a dead-end apology. **The real path is malformed argument JSON inside a normal `tool_calls` message** — `json.loads` raised, `except json.JSONDecodeError: arguments = {}`, and the tool executed with nothing. Both are now fixed: arguments **fail closed** (the model gets an error it can retry from), and truncated messages are discarded and the turn re-asked without tools, sharing one wrap-up path. Design §2.3, gap #4/#5 | `test_loop_truncated_message_does_not_execute_tools` — stub `llm.chat_completion` returning `finish_reason="length"` with a `tool_calls` fragment; assert `registry.execute_server_tool` not called |
| P0-2 | 🔴 | `app/agent/loop.py:140-148` | On the `asyncio.TimeoutError` path, `tool_result_events.pop(tc_id, None)` is in the `finally` but `tool_results` is not — a late device reply leaks an entry forever. Add `tool_results.pop(tc_id, None)` to the same `finally` | `test_device_tool_timeout_leaks_nothing` — assert both module dicts empty after a timed-out call |
| P0-3 | 🔴 | `app/agent/middleware`→`app/auth/middleware.py:36` | `f"{ts}{body.decode()}".encode()` raises `UnicodeDecodeError` → 500 on any non-UTF-8 body. HMAC the raw bytes instead: `ts.encode() + body` | `test_non_utf8_body_returns_401_not_500` |
| P0-4 | 🟠🔒 | `app/auth/middleware.py:33-42` | HMAC covers only `ts + body`, so a signed body replays to any endpoint within the ±300 s window. Sign `method + "\n" + path + "\n" + ts + "\n"` + raw body. **Breaking** — see the handshake note below | extend `tests/test_auth.py`: same body, different path → 401 |
| P0-5 | 🟠 | `app/config.py:12` | `API_SECRET_KEY = os.environ["API_SECRET_KEY"]` raises on *missing* but accepts `""` — and compose injects `""` when `.env` is absent, which authenticates. Reject anything under 32 chars at import with a clear message. **Also update `.github/workflows/deploy_agent_api.yml`**, which sets `API_SECRET_KEY: ci-test` (7 chars) and will start failing CI the moment this lands — change it to a 64-hex dummy | `test_short_api_secret_rejected` (import-time guard, so test the validator function not the import) |
| P0-6 | 🟠 | `app/main.py:14-17` | `logging.FileHandler("data/security.log")` is unbounded; every unauthenticated failed request appends, and the endpoint is internet-reachable. `RotatingFileHandler(maxBytes=10_000_000, backupCount=5)` | none needed; verify by hand |
| P0-7 | 🟡 | `app/config.py`, `app/agent/loop.py:55`, `app/tenants/models.py`, `docker-compose.yml`, `agent-api/tenants.json.example` | `MAX_TOOLS` (env `MAX_TOOL_CALLS_PER_TURN`, raised to 10 in 47e9c71) is spent as **loop iterations** at `loop.py:55`, but the design also needs a **tool-count** ceiling. Split into `MAX_TOOL_ROUNDS` (default 10, 3 for voice) and `MAX_TOOLS_IN_CONTEXT` (default 10). Add `max_tool_rounds` to `Tenant`, keep `max_tools` as a deprecated alias for one release. Design §2.5, §8.3 | `test_tenant_max_tool_rounds_alias` |
| P0-8 | 🔴 | `app/routes/chat.py:_legacy_turn` | The `DirStore` path already persists on disconnect via `try/finally` — `_legacy_turn` (sqlite/memory backends) does not. Mirror the same structure, or delete `_legacy_turn` if `SESSION_BACKEND` is never anything but `dir` in practice. Decide, don't leave both | `test_legacy_turn_persists_on_disconnect`, or delete the path and its tests |
| P0-9 | 🟠 | — | **Moved to PW-1.** Backups are unconditional and should not sit ninth in a phase; see "What survives the argument" | — |
| P0-10 | 🟠 | `app/auth/guest.py:47,119` + the pose-spatial-studio proxy | **The highest-severity live defect in the system, and it was missing from this plan entirely (objection 3).** The guest per-IP rate limit is structurally one global bucket: all production traffic egresses from the proxy's IP, so the limit is 3 guest sessions/hour *for the entire internet* — a trivial denial of service against the public demo. Key the limiter on `CF-Connecting-IP` and have the proxy forward the real client IP. Design §6.2 marks this HIGH and "not fixed by this design" | `test_guest_rate_limit_keys_on_forwarded_ip` |
| P0-11 | 🟠 | `routes/guest.py:36-38` + the proxy | The guest `Origin` check is nullified through a legitimate deployed path: the proxy forwards the client-supplied `Origin` verbatim. Pin the upstream `Origin` at the proxy instead of forwarding it. Design §6.2, MEDIUM, also "not fixed by this design" | `test_guest_rejects_unpinned_origin` |
| P0-12 | 🟡 | `app/skills/registry.py`, `app/skills/remember/__init__.py` | **Recovers half the tool budget in an hour (objection 4, 5).** `email`, `github_cli` and `gitlab_cli` cannot run in the deployed container — no `EMAIL_*` vars are passed and `gh`/`glab` were never installed (ANALYSIS.md §1.2) — yet they contribute 5 tools to every context window. `avatar_control` contributes 4 more and serves a different product. Either delete them or set them disabled-by-default in `data/skills.json` and exclude them from the personal tenant's `local_skills`. Sixteen tools against a design ceiling of ten (§8.3) is a real part of why tool selection is unreliable today. **Also restore `app/skills/remember/__init__.py`** — its absence meant the skill was never discovered and user memory was write-less (see objection 4). Net 16 → 13. **Deploy step, or this is inert:** `data/skills.json` is gitignored and bind-mounted, and the live copy already says `"email": true` etc. — written by `_save_state()` on first boot, not by you. Saved state deliberately beats the new default, so on the box you must PATCH `/api/v1/skills/{name}` to `{"enabled": false}` for the three, or delete those keys and restart | `tests/test_tool_budget.py` — 11 tests, ceiling asserted at 13 with the count in the failure message |

**The 🔒 handshake (P0-4).** Do not flag-day this. Ship the server accepting **both** signature schemes for one release, keyed on a new `X-Sig-Version: 2` header — absent means v1. Ship IndexApp sending v2. Then remove v1 acceptance in the following release and log any v1 request as `AUTH_WARN legacy_signature` in the meantime so you can see when the last old client goes away. Same pattern for every 🔒 below.

**Explicitly not in Phase 0:** the prompt-injection sanitiser (`app/agent/sanitize.py`). Its 7-regex denylist is weak, but rewriting it in isolation buys little. It splits in two, not one — see review finding #2: gateway-side sanitisation for anything that crosses the gateway (P1-7, MCP/server tool results) and the `after_tool_result` hook for device-tool results, which structurally never reach the gateway (P6-5). Leave the current single call site alone until both exist.

---

## Phase 0.5 — Interfaces and the typed domain model

**Goal:** four abstract base classes and one enum/dataclass vocabulary that every later phase builds against, so the gateway, the harness variants and the tool servers are implementations of a stated contract rather than four independently-invented shapes.
**Effort:** ~5–6 days, not the 3 first estimated — see review finding #8; PA-6 and PA-8 alone are plausibly a day or two each once tests are included. **Exit:** `mypy --strict` clean on `packages/sb-contracts/`; every existing concrete class in the table below declares its base; `pytest` green with no behaviour change; the Docker image still builds (PA-0).

> **⚠️ Demoted off the critical path — read objection 6 before starting this phase.**
> The original instruction here was "land this before Phase 1." That was wrong. You cannot design a good `Harness` ABC from one concrete implementation and two imagined ones — and tactical finding #9, which notes that `abort()`/`steer()` are meaningless for `ScheduledHarness`, is that error showing up *before any implementation exists*. The same applies to `ToolGateway`: its real shape will be obvious once the gateway is written, and retrofitting the ABC onto working code is an afternoon.
>
> **What to do instead:**
> - **Pull `FinishReason` and `ToolOutcome` into P0** (two hours). These two are not abstraction — they are string sentinels that have already caused a live bug (`finish_reason` unhandled at `loop.py:87`; `.startswith("Error")` as a type test at `loop.py:155`). They earn their place on correctness grounds alone.
> - **Write Phase 1's gateway with plain functions and concrete types.** Extract `ToolGateway` and `GatewayPolicy` afterwards, from code that exists.
> - **Defer the rest of this phase until there are two real implementations of something.** `Harness` becomes worth writing when `ScheduledHarness` (P3-6) and `ChatHarness` both exist and you can see what they actually share — which is likely less than this section assumes.
>
> Everything below stays as the *target* design for that later extraction. It is a good destination and a bad starting point. **PA-0 is the exception** — if you ever do create `packages/sb-contracts/`, the Docker build context must be fixed first or the image stops building (tactical finding #1).

### Module layout

A uv workspace member, so `agent-api` and `mcp-gateway` depend on one pinned contract package:

```
packages/sb-contracts/
    pyproject.toml           # name = "sb-contracts", no runtime deps beyond pydantic
    src/sb_contracts/
        enums.py             # every closed set of values in the system
        models.py            # wire types (pydantic) + internal value objects (dataclass)
        interfaces.py        # the four ABCs below
        errors.py            # typed exceptions
```

Root `agent-api/pyproject.toml` and `mcp-gateway/pyproject.toml` both get `dependencies = ["sb-contracts"]`, wired with `[tool.uv.workspace] members = ["agent-api", "mcp-gateway", "packages/*"]`.

### Enums

Python 3.12, so `enum.StrEnum` throughout — JSON-serialisable with no encoder, comparable to plain strings, which keeps the existing dict-shaped SSE payloads working unchanged.

| Enum | Members | Replaces today |
|---|---|---|
| `ExecutionSide` | `SERVER`, `DEVICE`, `GATEWAY` | the bare string `"server"`/`"device"` in `BaseSkill.execution_side` and the `execution_side ==` comparisons in `registry.py:140-157` |
| `ExecutionMode` | `PARALLEL`, `SEQUENTIAL` | nothing — new, needed by P6-2 |
| `TurnOrigin` | `VOICE`, `TEXT`, `SCHEDULED`, `EVENT` | nothing — new; drives `max_tool_rounds` (P0-7) and the thinking dial (P6-9) |
| `ThinkingLevel` | `OFF`, `LOW`, `HIGH` | the import-time boolean `LLM_ENABLE_THINKING` (`config.py`). Three levels, not Pi's seven — this system has one model |
| `ModelRole` | `CHAT`, `SUMMARISE`, `PHRASE` | nothing — new, needed by P6-9 |
| `FinishReason` | `STOP`, `TOOL_CALLS`, `LENGTH`, `ERROR` | the raw string at `loop.py:87` that P0-1 must branch on. Making it an enum is what stops the next person forgetting `LENGTH` again |
| `ToolOutcome` | `OK`, `ERROR`, `TIMEOUT`, `DENIED`, `INVALID_ARGUMENTS`, `CONFIRMATION_REQUIRED`, `BUDGET_EXCEEDED` | the `"Error: ..."` string-prefix convention in `client.py` and `loop.py:155` (`not result.startswith("Error")`), which is a sentinel pretending to be a type |
| `EntryType` | `MESSAGE`, `COMPACTION`, `BRANCH_SUMMARY`, `MODEL_CHANGE`, `BUDGET_CHANGE`, `CUSTOM`, `LABEL` | nothing — new, needed by P6-1 and P6-10 |
| `CompactionReason` | `MANUAL`, `THRESHOLD`, `OVERFLOW` | nothing — new, P6-1 |
| `ToolBand` | `CORE`, `AVAILABLE`, `LOADED` | the `always_available` boolean in `BaseSkill`, which P6-6 retires |
| `TierStatus` | `OK`, `DEGRADED`, `DOWN` | the boolean health check, which P3-7 splits per tier |
| `CacheState` | `FRESH`, `STALE`, `MISS` | nothing — new, P3-2. `STALE` is load-bearing: the degradation ladder serves stale data with a label rather than an error |

`SessionState` already exists in `app/session/state.py` with a forward-only transition function — move it into `enums.py` unchanged and keep `transition()` where it is. Do not redesign it in this phase.

### Data model

Two libraries, on a deliberate split:

- **pydantic `BaseModel`** for anything crossing a process boundary or parsed from config — it validates, and the boundary is where untrusted input arrives. Matches existing house style (`app/tenants/models.py`).
- **`@dataclass(frozen=True, slots=True)`** for internal value objects passed inside one process. No validation cost in the per-token hot path, and frozen means a tool result cannot be mutated between the sanitiser and the transcript.

| Type | Kind | Fields (abbreviated) | Replaces |
|---|---|---|---|
| `ToolDescriptor` | pydantic | `name`, `description`, `parameters` (JSON Schema), `side: ExecutionSide`, `mode: ExecutionMode`, `band: ToolBand`, `requires_confirmation: bool`, `timeout_s: int` | the raw OpenAI-shaped `dict` returned by `BaseSkill.get_tool_definitions()` and indexed as `t["function"]["name"]` in five places in `registry.py` |
| `ToolCall` | dataclass | `id`, `name`, `arguments: dict`, `ticket_id` | the `tc["function"]["arguments"]` dict-digging at `loop.py:94-99` |
| `ToolResult` | dataclass | `call_id`, `outcome: ToolOutcome`, `content: str`, `duration_ms`, `bytes`, `error: str \| None` | the `"Error: ..."` string convention |
| `TurnRequest` | pydantic | `session_id`, `tenant`, `user`, `origin: TurnOrigin`, `text \| audio_ref`, `max_tool_rounds`, `max_tokens`, `thinking: ThinkingLevel` | the loose kwargs on `run_agent_loop()` |
| `HarnessEvent` | dataclass hierarchy | `TokenEvent`, `ToolCallEvent`, `ToolResultEvent`, `ConfirmEvent`, `AvatarEvent`, `DoneEvent`, `ErrorEvent` — each with `.to_sse() -> str` | the hand-built `f"event: token\ndata: {json.dumps(...)}\n\n"` literals scattered through `loop.py` (nine sites) |
| `CompletionRequest` / `CompletionResponse` | pydantic | `messages`, `tools`, `role: ModelRole`, `thinking: ThinkingLevel`, `max_tokens` / `content`, `tool_calls`, `finish_reason: FinishReason`, `usage: Usage` | the raw payload dict in `llm.py:_build_payload` and `resp["choices"][0]["message"]` digging |
| `Usage` | dataclass | `input`, `output`, `total` | nothing — vLLM returns this today and it is discarded (P6-8) |
| `PolicyDecision` | dataclass | `allowed: bool`, `outcome: ToolOutcome`, `reason: str`, `confirmation_token: str \| None` | nothing — new, P1-2/P1-5 |
| `AuditRecord` | pydantic | `ticket_id`, `tenant`, `tool`, `args_hash`, `bytes`, `duration_ms`, `outcome: ToolOutcome`, `at` | nothing — new, P1-7 |
| `CacheEntry` | dataclass | `value`, `fetched_at`, `ttl_s`, `state: CacheState` | nothing — new, P3-2 |
| `TierHealth` | pydantic | `tier`, `status: TierStatus`, `detail` | the flat `/health` body, P3-7 |
| `SessionEntry` | pydantic | `id`, `parent_id`, `type: EntryType`, `at`, payload union | the untyped `history.jsonl` lines, P6-1/P6-10 |

`MCPServerConfig` and `Tenant` (`app/tenants/models.py`) are already pydantic and stay — but `MCPServerConfig` moves to `sb-contracts` since the gateway now owns it (P1-10).

### The four abstract base classes

```python
# sb_contracts/interfaces.py
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

class MCPClient(ABC):
    """One transport to one MCP endpoint. Never raises for tool-level
    failures — returns a ToolResult carrying the outcome."""

    @abstractmethod
    async def list_tools(self) -> list[ToolDescriptor]: ...

    @abstractmethod
    async def call_tool(self, call: ToolCall, *, timeout_s: int) -> ToolResult: ...

    @abstractmethod
    async def health(self) -> TierHealth: ...

    @abstractmethod
    async def aclose(self) -> None: ...


class MCPToolServer(ABC):
    """A tool server this project owns. Subclasses declare tools and execute
    them; the FastMCP wiring is provided by the base class."""

    name: str

    @abstractmethod
    def descriptors(self) -> list[ToolDescriptor]: ...

    @abstractmethod
    async def execute(self, call: ToolCall) -> ToolResult: ...

    @abstractmethod
    async def health(self) -> TierHealth: ...

    def mount(self, app) -> None:
        """Default: bind descriptors() and execute() to a FastMCP route."""


class ToolGateway(ABC):
    """What a harness depends on to reach any tool. The harness never holds
    a credential and never chooses a transport."""

    @abstractmethod
    async def catalog(self, tenant: str, query: str | None = None) -> list[ToolDescriptor]: ...

    @abstractmethod
    async def call(self, tenant: str, call: ToolCall) -> ToolResult: ...

    @abstractmethod
    async def read(self, tenant: str, call: ToolCall) -> ToolResult:
        """Read-only tools only — the dashboard refresh path (P3-3)."""


class Harness(ABC):
    """One agent runtime. A turn in, an event stream out."""

    @abstractmethod
    def run(self, turn: TurnRequest) -> AsyncIterator[HarnessEvent]: ...

    @abstractmethod
    async def abort(self, session_id: str) -> bool: ...

    @abstractmethod
    async def steer(self, session_id: str, text: str) -> bool: ...
```

Plus a fifth that falls out of the gateway design and is worth naming, because it turns design §4.2's numbered list into executable units:

```python
class GatewayPolicy(ABC):
    """One stage of the gateway pipeline. Stages compose in order."""

    @abstractmethod
    async def check(self, ctx: PolicyContext) -> PolicyDecision: ...
```

### Adoption — what implements what

| ABC | Implementations | Phase |
|---|---|---|
| `MCPClient` | `StreamableHTTPClient` (today's `app/mcp/client.py`, unchanged behaviour), `GatewayClient` (agent-api → gateway), `FakeMCPClient` (tests) | 0.5 / P1-9 |
| `MCPToolServer` | `DarwinServer`, `WeatherServer`, and the existing `workspace/mcp_server/` wcc groups refactored onto it | P2-1, P2-2 |
| `ToolGateway` | `HttpToolGateway` (prod), `DirectToolGateway` (dev — in-process, no second service, so the box is not required to run two apps to debug one), `FakeToolGateway` (tests) | P1-8, P1-9 |
| `GatewayPolicy` | `AllowlistPolicy`, `SchemaPolicy`, `ConfirmationPolicy`, `BudgetPolicy`, `SecretInjectionPolicy`, `EgressPolicy`, `SanitizePolicy` — one per numbered responsibility in design §4.2 | P1-2…P1-7 |
| `Harness` | `ChatHarness` (today's `run_agent_loop`, wrapped), `VoiceHarness` (3 rounds, thinking off, sentence-wise TTS), `ScheduledHarness` (template-first, model optional — §2.4) | 0.5 / P3-6 / P7-3 |

`BaseSkill` (`app/skills/base.py`) is already an ABC and stays. Change only its property types to the new enums (`execution_side: ExecutionSide`) and add `execution_mode: ExecutionMode` for P6-2.

**`ScheduledHarness` is the one that justifies the whole phase.** The tier split in design §2.2 says a scheduled train push must work with vLLM down. Today that is a statement of intent with no type behind it; as a `Harness` implementation whose `run()` reaches the model only for the "unusual result" phrasing call, it is a class you can unit-test with a `FakeLLM` that raises on every request.

### Tasks

| # | Where | Change |
|---|---|---|
| PA-0 | `.github/workflows/deploy_agent_api.yml`, `agent-api/Dockerfile` | **Do this before PA-1 or the image stops building.** `docker/build-push-action` currently builds with `context: agent-api`; a repo-root `packages/sb-contracts/` is outside it. Change to `context: .`, `file: agent-api/Dockerfile`, and add `COPY packages/sb-contracts ./packages/sb-contracts` before `uv sync --frozen` in the Dockerfile. Mirror the same context change for the future `mcp-gateway` workflow (P1-11) (review finding #1) |
| PA-1 | `packages/sb-contracts/` | Create the workspace member; `enums.py` per the table; `mypy --strict` in CI for this package only |
| PA-2 | `packages/sb-contracts/models.py` | The type table above. pydantic at boundaries, frozen dataclasses inside |
| PA-3 | `packages/sb-contracts/interfaces.py` | The five ABCs |
| PA-3b | `mcp-gateway/app/dev_gateway.py` | `DirectToolGateway.__init__` raises unless `SB_ALLOW_DIRECT_GATEWAY=1` is set, so the in-process dev convenience cannot become production configuration by omission (review finding #6) |
| PA-4 | `app/mcp/client.py` | Declare `StreamableHTTPClient(MCPClient)`. Return `ToolResult` instead of `"Error: ..."` strings; keep a `.content` string for callers not yet migrated |
| PA-5 | `app/agent/loop.py` | Wrap the existing generator as `ChatHarness(Harness)`. **No behaviour change in this task** — pure extraction, so the diff is reviewable and a regression is obvious |
| PA-6 | `app/agent/loop.py` | Replace the nine hand-built SSE f-strings with `HarnessEvent.to_sse()`. **Touches the IndexApp wire contract (review finding #3) — not a 🔒 handshake, a byte-identical requirement:** capture real SSE output from the current code as a fixture first, assert the refactored output matches byte-for-byte, and only then delete the f-strings |
| PA-7 | `app/skills/base.py`, `app/skills/registry.py` | `ExecutionSide` enum on `BaseSkill`; `registry.py` stops comparing bare strings |
| PA-8 | `app/agent/llm.py` | `CompletionRequest`/`CompletionResponse`/`FinishReason`/`Usage`. This is the task that makes P0-1 and P6-8 one-liners instead of archaeology |
| PA-9 | `.github/workflows/deploy_agent_api.yml` | Add a `mypy --strict packages/sb-contracts` step before the test step. Strict on the contracts package only — strict on all of `app/` is a different project |

**Sequencing, if this phase ever runs.** PA-0 first or the image stops building. PA-1…PA-3 are one PR; PA-4…PA-8 are one PR each and each must be behaviour-preserving — if a PR in this phase changes what the system does, it is the wrong PR.

`FinishReason` and `ToolOutcome` are **no longer part of this phase** — they moved to P0, where they belong on correctness grounds without waiting for a contracts package. PA-8 is correspondingly smaller: it is now only the `CompletionRequest`/`CompletionResponse`/`Usage` payload modelling.

---

## Phase 1 — MCP gateway

**Goal:** `agent-api` holds no external credential and needs no route to the internet. Design §4.2.
**Effort:** ~5 days. **Exit:** *before* P1-9's cutover, `agent-api`'s test suite passes with `MCP_ALLOWED_PRIVATE_HOSTS` empty (P1-9 deletes `ssrf.py`, the only reader of that variable, so this check has nothing left to run against afterward — verify it pre-cutover, not post); every MCP token removed from `agent-api`'s config; the gateway rejects an out-of-allowlist tool and a schema-invalid argument set. **Caveat (review finding #5):** the allowlist enforced here is a policy check in application code, not a network boundary — a compromised gateway process can still reach anything its host can reach until Phase 4's jail network isolation ships. Don't read this phase as "credentials are contained"; read it as "credentials are centralised and policy-checked."

**New component.** A second FastAPI app at `mcp-gateway/` in this repo — same CI workflow shape, same `uv` + GHCR pipeline, separate image.

| # | Where | Change |
|---|---|---|
| P1-1 | `mcp-gateway/app/mcp/client.py` | **Copy** `agent-api/app/mcp/client.py` (71 lines) rather than sharing it — it will diverge, since the gateway adds secret injection and per-call timeouts the agent side must not have. Both copies implement `MCPClient` from `sb-contracts` (Phase 0.5), so the *contract* is shared even though the transport code is not. That split is the point: share the types, not the plumbing |
| P1-2 | `mcp-gateway/app/policy.py` | Move `Tenant.allowed_skill_names()` enforcement here. `agent-api` may *ask* for any tool; the gateway decides. Load `gateway.json`: `{servers: {...}, tenants: {name: {tools: [...], monthly_budget: int}}}` |
| P1-3 | `mcp-gateway/app/catalog.py` | `GET /catalog?tenant=&query=` → tenant-scoped descriptors in three bands (core / available / loaded, design §4.3). Reuses `MCPProxySkill`'s background-refresh pattern from `agent-api/app/mcp/proxy_skill.py` — a down server contributes zero tools, never blocks |
| P1-4 | `mcp-gateway/app/validate.py` | JSON Schema validation of arguments **before** the upstream call. Also **flatten nested schemas** on the way out and re-nest on the way in — Qwen3-14B handles flat argument schemas far better than nested ones. Design §4.2 step 2, gap #5 |
| P1-5 | `mcp-gateway/app/confirm.py` | `requires_confirmation: bool` per descriptor. A gated call returns `{"status":"confirmation_required","summary":...}` plus a single-use `confirmation_token` (HMAC, 120 s TTL) instead of executing. Design §4.2 step 3 |
| P1-6 | `mcp-gateway/app/budget.py` | Per-tenant, per-tool monthly counters with an 80% alarm. This is what makes Maps Grounding's 10K free tier enforceable rather than hoped-for |
| P1-7 | `mcp-gateway/app/audit.py`, `app/sanitize.py` (new) | Append-only audit record `{ticket_id, tenant, tool, args_hash, bytes, duration_ms, outcome}` — `args_hash` not `args`, tool arguments contain the user's locations — **and** result-side sanitisation + size cap on every value returned from `/call` and `/read`. This is where `app/agent/sanitize.py`'s injection-denylist logic actually lives once MCP tools stop being reached directly (review finding #2); `agent-api`'s copy stays only for device-tool results (P6-5) |
| P1-8 | `mcp-gateway/app/main.py` | `POST /call`, `GET /read` (read-only tools, for the Phase 3 dashboard refresh), `GET /catalog`. Auth: a gateway token distinct from every tenant key |
| P1-9 | `agent-api/app/mcp/gateway_skill.py` | Replace `MCPProxySkill` registration in `app/main.py:56-66` with a single `GatewaySkill` that fetches `/catalog` and routes `execute()` to `/call`. `app/mcp/client.py`, `proxy_skill.py` and `ssrf.py` can then be deleted from `agent-api` — **delete them, don't leave them dormant** |
| P1-10 | `agent-api/app/tenants/models.py`, `data/tenants.json` | Remove `MCPServerConfig.auth_token` and the whole `mcp_servers` map from the agent side. Tenants keep `name`, `user`, `api_key`, `origins`, `system_prompt`, `local_skills`, budgets. Update `tenants.json.example` and the three wcc entries |
| P1-11 | `.github/workflows/deploy_gateway.yml` | New workflow mirroring the agent one, **plus** two smoke tests the design calls for: assert the egress allowlist is non-empty and that `/call` rejects an out-of-allowlist tool. The gateway now holds every credential; its gate should be stricter than the agent's, not looser |

**Sequencing note.** P1-9 and P1-10 are the cutover and must land together. Until they do, run the gateway in parallel with the existing direct MCP path and compare results on the wcc tenants — that is the cheapest correctness check available, since those three servers are already exercised in anger.

**Do not** put the TTL cache in the gateway (design §3.6): a cache there makes "was this tool actually called" unanswerable from the audit log. Cache lives in `agent-api`, Phase 3.

---

## Phase 2 — Tool layer

**Goal:** the four v1 capabilities answerable from a command line, through the gateway, with no model involved.
**Effort:** ~4 days. **Exit:** `curl` against the gateway returns correct departures, weather and places.

| # | Where | Change |
|---|---|---|
| P2-0 | `app/skills/registry.py:112-137`, gateway `/catalog` | **Promoted from P6-6 — this must land before new tools do (objection 4).** Twenty tools already exceed the design's ten-tool ceiling; Phase 2 adds Darwin, weather and maps on top of that. Retire the keyword prefilter and `always_available` (every MCP proxy sets it `True`, so today the filter only prunes local skills). Bands: core ≤6 full schemas, available = name + one line, loaded = full schema after a `load_tool(name)` meta-tool call. With P0-12's cleanup this is the difference between a workable catalogue and one the model picks from at random |
| P2-1 | `mcp-servers/darwin/` | FastMCP server over **Darwin LDBWS via the Rail Data Marketplace REST API**. Not legacy SOAP OpenLDBWS — those tokens no longer work; the consumer key comes from the marketplace subscription. Tools: `departures(from, to?, when?)`, `service_detail(service_id)`. Flat arguments only |
| P2-2 | `mcp-servers/weather/` | Thin Open-Meteo wrapper. No API key. Tools: `current(lat, lon)`, `forecast(lat, lon, days)` — take coordinates, not place names; resolution is the maps server's job |
| P2-3 | config only | Register **Google Maps Grounding Lite** MCP off the shelf. Returns place IDs, coordinates and Maps links, which is what makes "show them on a map" trivial; it also covers routes, so it does double duty |
| P2-4 | `mcp-gateway/gateway.json` | Per-server egress allowlist: `darwin` → Rail Data Marketplace only, `weather` → `api.open-meteo.com` only, `maps` → Google endpoints only. **This is a config check in the gateway's own HTTP client, not a kernel-enforced boundary, until P4-7's jail network isolation exists (review finding #5)** |
| P2-5 | `mcp-servers/*/README.md` | For each: the exact `curl` that proves it works, and the credential it needs. This is the artefact Phase 2's exit criterion is measured against |

**Open question blocking P2-3:** which Brussels/area filters matter. Filtering happens in tool code, not in the model (principle 1), so the filter set has to be specified before the tool is written. Design §11.7.

---

## Phase 3 — Deterministic tier

**Goal:** useful things happen with vLLM stopped. Design §2.4.
**Effort:** ~5 days. **Exit:** a morning train notification arrives on the iPhone with the inference tier shut down.

| # | Where | Change |
|---|---|---|
| P3-1 | `agent-api/app/schedule/` | APScheduler in-process, `AsyncIOScheduler`, jobs defined in code (versioned with the repo) not a crontab. Job store on the `data/` volume so a restart doesn't drop a schedule |
| P3-2 | `agent-api/app/cache/` | TTL cache keyed on `(tool, canonicalised args)`, in-memory with disk write-through under `data/cache/`. TTLs per design §3.6: departures 60 s, weather 15 min, places 24 h, routes 1 h |
| P3-3 | `agent-api/app/routes/state.py` | `GET /api/v1/state` — reads cache only, **never** triggers a tool call. Refresh is P3-1's job. This is what keeps Maps usage flat regardless of panel uptime |
| P3-4 | `agent-api/app/routes/event.py` | `POST /api/v1/event` for external triggers (iOS Shortcuts geofence, webhooks). Same auth as `/chat` |
| P3-5 | `agent-api/app/push/apns.py` | APNs direct with the developer-signed cert (token-based auth, `.p8`). ntfy only if APNs setup proves awkward |
| P3-6 | `agent-api/app/schedule/jobs/trains.py` | The morning reminder, implemented as `ScheduledHarness(Harness)` (Phase 0.5) so it is unit-testable against a `FakeLLM` that raises on every call: read cache → format from a **template, in pure code** → push. Ask the LLM for one sentence only when the result is unusual (cancellation, platform change), and fall back to the template when inference is unavailable |
| P3-7 | `agent-api/app/routes/health.py` | Extend `/health` to report tier status separately: `always_on: ok` / `inference: down` should be a 200, not a failure. Today an unreachable vLLM makes the whole system look dead |

**This is the phase that proves the thesis.** If a notification arriving unasked is not useful on its own, the LLM will not rescue it — and that is worth knowing before Phases 4–7 are spent.

---

## Phase 4 — microVM migration

**Goal:** delete the Docker socket and the agent's default route. Design §5.
**Effort:** ~10–12 days, not the 5+1 first estimated — see review finding #8. This is Kata + Cloud Hypervisor tooling nothing in this tree has touched before (Docker Compose end to end today), against design §5.3's own advice to go slowly. **Exit:** `agent-api` runs with no default route and Phases 1–3 still pass their exit criteria.

| # | Where | Change |
|---|---|---|
| P4-1 | host | Install Kata Containers with the **Cloud Hypervisor** backend. Keep containerd; the GHCR images from PR #40 are unchanged — this is a `runtimeClassName` swap, which is the entire reason for choosing Kata over hand-rolled Firecracker |
| P4-2 | `deploy/systemd/sb-*.service` | Units for `sb-edge`, `sb-gw`, `sb-agent`. Start order `sb-edge → sb-gw → sb-agent`; `sb-infer` independent. Replaces `docker compose up -d` |
| P4-3 | `deploy/network/` | p2p virtio-net links: `sb-agent ↔ sb-gw`, `sb-agent ↔ sb-infer`. **`sb-gw` is the only VM with a default route.** Jail bridge with per-VM egress allowlists |
| P4-4 | `agent-api/app/agent/llm.py:57-80` | **Delete `_start_local_llm()`** and the `ConnectError`-triggered auto-start. This is `ANALYSIS.md`'s highest-severity finding and it does not survive the migration anyway. Keep the 502/503/504 retry with backoff — that part is good |
| P4-5 | `docker-compose.yml` | Remove the `/var/run/docker.sock` mount (line 65) and the `extra_hosts: host.docker.internal` entries. Retire the file once P4-2 is proven; keep it tagged in git as the rollback |
| P4-6 | host | vLLM, faster-whisper and Kokoro as systemd units on the **host**, bound to the `sb-agent` p2p address — **Phase A of design §5.3**. Do *not* do GPU passthrough yet: `sb-infer` holds no credentials and has no egress, so VFIO buys little against IOMMU setup, a host that loses its GPU, and consumer-card reset quirks |
| P4-7 | jails | One read-only-rootfs microVM per MCP server, tmpfs only, pre-warmed pool of 2 per server (design §6.3 targets <200 ms cold) |
| P4-8 | `deploy/README.md` | The runbook: deploy, rollback-by-retag, restore from P0-9's backup, and what to do when a jail wedges |

**Rollback plan — corrected (objection 8).** The original "`systemctl stop sb-*` then `docker compose up -d` at the previous tag" does not work: P4-6 moves vLLM to a host systemd unit, so the old compose file's `LLM_URL=http://secondbrain-llm:8080` points at a container that no longer exists on a network with no members. The real rollback has to unwind P4-6 too:

1. `systemctl stop sb-agent sb-gw sb-edge`
2. `systemctl stop sb-vllm` (the host unit from P4-6)
3. `git checkout <pre-P4-tag> -- docker-compose.yml` and `docker compose --profile local-llm up -d` — which restores the `secondbrain-llm` container the old `LLM_URL` expects
4. Retag `agent-api` to a pre-P4-4 image, since the current one has had `_start_local_llm()` deleted

**Rehearse steps 1–4 on the box before starting P4-1**, not after P4-2 has failed at midnight. If rehearsing it is too disruptive to attempt, that is information about whether this phase should happen at all.

---

## Phase 5 — Panel

**Goal:** the docked iPad. **Effort:** ~3 days.

| # | Where | Change |
|---|---|---|
| P5-1 | `dashboard/` | PWA reading `GET /api/v1/state`, polling 60 s. Wall and tablet layouts |
| P5-2 | iPad | **Autonomous Single App Mode** via Apple Configurator — not Guided Access, which does not survive a restart |
| P5-3 | `dashboard/` | Local-first then tailnet: serve on both a static LAN address and the tailnet name, client tries LAN first. A panel on a wall must not go blank when a coordination server hiccups |
| P5-4 | hardware | Dock that cuts power on a schedule, or hold charge near 80%. Continuous mic + permanently-lit display + charging = a warm battery that throttles and ages |

---

## Phase 6 — Harness maturity

**Goal:** the Pi-derived gaps. Design §9. **Effort:** ~8 days. Ordered within the phase by value.

| # | Gap | Where | Change |
|---|---|---|---|
| P6-1 | #2 | `agent-api/app/session/context.py` (new), `app/agent/loop.py:57` | **Compaction.** Replace `history[-20:]` with `build_context_entries()`: walk leaf→root to the latest `compaction` entry, return `[compaction, …from first_kept_entry_id, …after]`. Settings for a 32k window: `reserve=4096`, `keep_recent=6000`. `find_cut_point()` must **never cut at a tool result** — an orphaned tool result without its call is a malformed request. Summary skeleton is assistant-shaped, not Pi's coding-shaped: `## What the user asked for`, `## Commitments made`, `## Facts learned about the user`, `## Open threads`. Run as its own request with a fresh session id and no cache write. **Degraded path:** if vLLM is down at the threshold, truncate but write a visible `compaction` entry reading `(context truncated — summariser unavailable)`. Silent forgetting is the failure to avoid |
| P6-2 | #6 | `app/agent/loop.py:93-159`, `app/skills/base.py` | **Parallel tool batches.** `asyncio.gather` over the batch. Two opt-outs: `execution_side == "device"` stays serial (two concurrent EventKit prompts is worse than the latency saved), and `ToolDescriptor.mode` (`ExecutionMode`, Phase 0.5) defaults to `SEQUENTIAL` for anything `requires_confirmation`. Mixed batch = parallel members together, then sequential members in order |
| P6-3 | #7 | `app/agent/llm.py:96`, `app/agent/loop.py:170,205` | **Real streaming — measure first.** Note the trap: `loop.py:68` is the only per-round completion and it *always* carries `tools`; the only tools-free call is the wrap-up at `loop.py:190`. So streaming the user-visible answer means streaming with tools in context, which is the hermes-parser risk. Step 1 is an afternoon of testing the deployed build. If sound → stream every round. If not → hold-back buffer (64 tokens or a sentence boundary; abandon and re-issue non-streaming if the prefix looks like a raw tool call). Either way `finish_reason == "length"` stays authoritative (P0-1) |
| P6-4 | #10 | `app/routes/sessions.py`, `app/agent/loop.py` | **Abort.** `POST /api/v1/sessions/{id}/abort` sets an `asyncio.Event` checked at each round boundary and passed as a signal into tool execution; gateway propagates as MCP cancellation and destroys the jail if the upstream ignores it. Currently there is no `abort`/`cancel` path anywhere in `routes/` or `agent/`, and `wcc-events` has `timeout_s: 2700` |
| P6-5 | #11 | `app/agent/hooks.py` (new) | **Hook bus**, four events: `before_tool_call` (confirmation, redaction, budget), `after_tool_result` (this is where `sanitize()` moves, plus size cap and fact extraction), `before_llm_request` (token accounting), `turn_settled` (memory write-back, ticket finalisation). Reuse the `ContextVar` pattern already in `app/skills/base.py:6-20` to carry session and user |
| P6-6 | #8 | — | **Moved to P2-0.** Progressive disclosure cannot wait until Phase 6 when Phase 2 adds tools to an already-over-budget catalogue (objection 4) |
| P6-7 | #9 | `app/routes/steer.py` (new), `app/agent/loop.py:160` | **Steering.** `POST /api/v1/steer {session_id, text}` → per-session queue, drained at the `continue` at `loop.py:160`. `SessionQueue` already serialises per session, so no new concurrency model |
| P6-8 | #12 | `app/agent/llm.py`, `app/session/ticket.py` | **Token accounting.** vLLM returns `usage` on every response and it is currently discarded; PA-8 already gives it a `Usage` home. Record `{input, output, total}` per ticket; surface context-fill % so P6-1's threshold is observable |
| P6-9 | #13, #14 | `app/agent/llm.py`, `app/config.py` | **Model roles + thinking dial.** Roles `chat` (512–1024), `summarise` (2048, used by P6-1), `phrase` (64, used by P3-6). Thinking becomes per-turn, keyed on a `voice`\|`text` origin the client sends: **off for voice, non-negotiable** (§8.1); on for typed. Replaces the import-time `LLM_ENABLE_THINKING` boolean |
| P6-10 | #15, #16 | `app/session/session_dir.py`, `app/routes/sessions.py` | ⚪ **Branching + change records.** `parent_id` on entries, `POST /sessions/{id}/fork {from_entry_id}`, `branch_summary` on leave; `model_change`/`budget_change` entries so a resumed session restores what it ran under. **Last, and genuinely skippable** — these are coding-agent ergonomics from Pi's problem domain. Do #16 only once #14 exists, or a resumed session silently inherits the wrong default |

---

## Phase 7 — Voice

**Goal:** spoken question → spoken answer under three seconds. **Effort:** ~5 days.

| # | Where | Change |
|---|---|---|
| P7-1 | host | faster-whisper on the GPU box. Server-side because UK station names and Brussels street names are exactly where on-device recognition fails. On-device WhisperKit stays as the degradation path |
| P7-2 | host | Kokoro TTS. Piper was archived Oct 2025 and is read-only — do not start there. `AVSpeechSynthesizer` stays as fallback |
| P7-3 | `app/routes/turn.py` (new), `app/agent/voice.py` | `POST /api/v1/turn` accepting audio or text, returning SSE + audio. `VoiceHarness(Harness)` (Phase 0.5) — `max_tool_rounds = 3` for voice-originated turns (P0-7), thinking off (P6-9) |
| P7-4 | IndexApp 🔒 | App Intent registered with `INAlternativeAppNames` = "Jarvis" + pronunciation hint. The `\(.applicationName)` token is mandatory in the phrase — omitting it is a compile error. Action Button / Back Tap as the silent fallback. **Background audio mode is not the starting point**: interruption handling for calls and Siri, permanent mic indicator, dies on reboot, real battery cost |
| P7-5 | IndexApp 🔒 | Conversation state fully server-side. Force-quit mid-turn on the iPhone, resume identically on the iPad. Blocked on open question §11.2 — whether SwiftData currently holds anything authoritative |
| P7-6 | `app/push/`, TTS | Hand TTS each sentence as it closes rather than the whole reply (depends on P6-3) |

---

## Cross-cutting

### Docs that are actively misleading (fix as you touch each area)

| File | Wrong | Fix in phase |
|---|---|---|
| `CLAUDE.md` | ~~Points every agent doc at a directory that does not exist~~ — **corrected 17 Sept**. Precise version: the directory *does* exist but holds only `SecondBrain.code-workspace`; the three doc paths in its table were never there. Table now points at `.claude/skills/second-brain/`, with the two stale docs labelled inline, plus a pointer to SYSTEM_DESIGN/WORK_PLAN | ✅ done |
| `README.md` | "ElevenLabs TTS" (actually on-device `AVSpeechSynthesizer`), "SSE-Starlette" (declared in `pyproject.toml`, unused in `app/`), "Docker Compose (3 containers)" (five) | P4 |
| `.claude/skills/second-brain/PROJECT_STRUCTURE.md` | Describes removed Gemini/llama.cpp design and presents unmerged multi-agent session design as live | P6 |
| `PLAN.md` | llama.cpp/GGUF architecture, stale checkboxes | P4 |
| `app/config.py` `SYSTEM_PROMPT` | Tells the model its only tools are calendar/reminders/contacts/clipboard/web-search — omitting email, github_cli, gitlab_cli, avatar_control. Plausibly degrades tool selection today | P6-6, with progressive disclosure |
| `ANALYSIS.md` | Says "zero tests"; there are now 12 files in `agent-api/tests/` | P0 — add a dated note rather than rewriting history |

### Decisions needed before the phase that blocks on them

| Decision | Blocks | Design ref |
|---|---|---|
| Which area/cuisine filters matter for place search | P2-3 | §11.7 |
| Does IndexApp's SwiftData hold anything authoritative? | P7-5 | §11.2 |
| Audio, transcript, or full turn on the wire? | P7-3 | §11.3 |
| Resident or on-demand vLLM (needs a real idle-draw measurement) | P4-6 | §11.4 |
| `feat/multi-agent`: rebase what remains, or close it? Much of its session machinery has since landed independently; P6-5 and P6-9 cover part of what it was for | P6 | §11.6 |

### Dependency graph

```
PW ──► P0 ──┬─► [PW-4 says yes?] ──► P1 ──► P2 ──► P3 ──┬─► P4 ──► P5
            │                                            │
            ├─────────────────────► P6-1 (compaction) ───┤
            │                                            │
            └─────────────────────────────────────────── ┴─► P6 ──► P7
                                                              ▲
                                                     [PW-2 says yes?]
```

Two gates, not one. **PW-4** decides whether Phases 1–5 happen at all; **PW-2** decides whether Phase 7 is a three-second product or needs rescoping before it is built. P0.5 is no longer on this graph — see the note at the head of that phase; extract its interfaces from working code when there is working code to extract them from. P6-1 (compaction) hangs off P0 directly because it is the best single product fix in the document and depends on nothing but the correctness work.

### The minimum viable path

If PW-4 comes back negative, or if you simply want the smallest thing worth doing, this is it — **roughly 8 days against the full plan's 50–60**:

| Do | Why | Days |
|---|---|---|
| PW-1 backup | the only irreversible failure in the system | 0.5 |
| P0-1, P0-2, P0-3, P0-8 | live correctness bugs, independent of everything | 1 |
| P0-5, P0-6, P0-10, P0-11 | confirmed security defects, one of them HIGH | 1.5 |
| P0-12 + P2-0 | recovers the tool budget; makes tool selection reliable | 1.5 |
| P6-1 compaction | stops the assistant forgetting you at message 21 | 3 |
| P6-3 streaming *(if PW-2 says the latency is there)* | removes ~1s of dead air per turn | 1 |

That path leaves you with the assistant you have, working correctly, remembering you, and backed up. Everything beyond it — gateway, new tools, proactive tier, microVMs, voice — is genuinely optional, and the honest framing of this whole document is that it is a menu, not a sequence.
