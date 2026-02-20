---
name: develop
---

Accept user requirement **$ARGUMENTS** following the full development workflow.

## Workflow Overview

**CRITICAL: Steps 1–5 are MANDATORY and BLOCKING. You MUST complete each step before proceeding to the next. Do NOT skip Steps 4 or 5.**

| Step | Action | Mandatory | Notes |
|------|--------|-----------|-------|
| 1 | **Understand** | YES | Analyze requirements, define acceptance criteria |
| 2 | **Branch** | YES | Create a properly named branch from the correct base |
| 3 | **Implement** | YES | Make changes following code conventions and lint standards |
| 4 | **Validate** | **YES — BLOCKING** | Run ALL test steps — NO exceptions |
| 5 | **Document** | **YES — BLOCKING** | Update CHANGELOG.md, PROJECT_STRUCTURE.md, README.md |
| 6 | **Review** | Optional | Optionally run a developer code review |
| 7 | **Commit** | YES | Stage files and commit with conventional message format |
| 8 | **Push & PR** | YES | Push branch and create pull request |

---

## Project Architecture

```
Local machine                    GitHub                     Server (beast)
┌──────────┐    git push     ┌──────────┐   CI/CD via    ┌──────────────────────┐
│ Edit code │ ──────────────▶│ SecondBrain│  Cloudflare   │ secondbrain-agent-api │
│ locally   │                │ repo      │ ──SSH tunnel──▶│ (always running)      │
└──────────┘                 └──────────┘                 │                       │
                                                          │ secondbrain-llm       │
                                                          │ (always running)      │
                                                          │                       │
                                                          │ secondbrain-cloudflared│
                                                          │ (always running)      │
                                                          └──────────────────────┘
```

### Containers

| Container | Purpose | Restart on deploy? |
|---|---|---|
| `secondbrain-agent-api` | FastAPI agent loop — auth, tools, LLM orchestration | YES — code deployed here via CI |
| `secondbrain-llm` | llama.cpp server — Qwen3 14B on RTX 5080 | NO — model reload takes 10-30s, only restart if config changes |
| `secondbrain-cloudflared` | Cloudflare Tunnel — networking only | NO — never needs code changes |

### Key paths

| Location | Path |
|---|---|
| Repo root | `~/data/SecondBrain/` |
| Agent API code | `~/data/SecondBrain/agent-api/` |
| Models (gitignored) | `~/data/SecondBrain/models/` |
| Container code path | `/root/agent-api/` (inside secondbrain-agent-api) |
| Persistent data | `~/data/SecondBrain/agent-api/data/` |
| GitHub Actions | `.github/workflows/deploy_agent_api.yml` |

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check (also checks LLM connectivity) |
| `/chat` | POST | Main conversation endpoint (SSE streaming) |
| `/tool_result` | POST | iPhone sends tool execution results back |

---

## Step 1: Understand the Requirement

Thoroughly analyze the requirement by:
1. Breaking down the request into discrete components
2. Identifying acceptance criteria, dependencies, and affected containers
3. Determining if changes affect only `agent-api/`, or also `docker-compose.yml`, `.env`, or Cloudflare config
4. Reading [.claude/PROJECT_STRUCTURE.md](.claude/PROJECT_STRUCTURE.md) to understand the project architecture

Document the following:
- Summary of the requirement
- Acceptance criteria (what defines "done")
- Which container(s) are affected
- Whether a container restart or rebuild is needed

---

## Step 2: Create Feature Branch

### Prepare the base

```bash
git fetch origin
```

Determine the base branch:
- If `staging` exists, branch from it
- Otherwise branch from `main`

### Create the branch

```bash
git checkout staging  # or main
git pull origin staging
git checkout -b type/brief-description
```

**Git flow:** `feature branch` → PR to `staging` → CI auto-deploys & test → PR to `main` → production

### Branch naming: `type/brief-description`

| Type | Purpose | Example |
|------|---------|---------|
| `feat/` | New feature | `feat/web-search-tool` |
| `fix/` | Bug fix | `fix/sse-streaming-timeout` |
| `docs/` | Documentation | `docs/update-api-docs` |
| `refactor/` | Code restructuring | `refactor/extract-tool-registry` |
| `security/` | Security improvement | `security/rate-limit-per-session` |

**Rules:** lowercase, hyphens (not underscores), keep it brief.

---

## Step 3: Implement Changes

All agent-api code lives in `agent-api/`. The primary file is `main.py`.

Follow these guidelines during implementation:

1. **Code conventions** — Maintain consistent formatting and style with existing codebase
2. **Acceptance criteria** — Ensure all criteria from Step 1 are fully met
3. **Security first** — Never weaken auth, input validation, or prompt injection defenses
4. **LLM isolation** — The `secondbrain-llm` container must NEVER have internet access. Do not add it to `tunnel_network`
5. **SSE compliance** — All streaming endpoints MUST use POST (not GET) due to Cloudflare Tunnel buffering bug. Always include `SSE_HEADERS`
6. **Deployment pipeline** — If you add Python dependencies, update the `pip install` line in both `docker-compose.yml` (container command) and `.github/workflows/deploy_agent_api.yml` (deploy step)

### Backend lint

Compile-check all modified Python files:
```bash
cd agent-api && python -m py_compile main.py
# Also check any new modules:
# python -m py_compile tools/web_search.py
```

### Security rules for agent-api code

- All endpoints (except `/health`) MUST use the `verify` auth dependency
- Tool results MUST pass through `sanitize()` before being fed to the LLM
- Input length MUST be enforced (`MAX_INPUT`)
- Tool call loops MUST be bounded (`MAX_TOOLS`)
- Never log secrets, tokens, or full request bodies
- `.env` must remain chmod 600 and in `.gitignore`

---

## Step 4: Validate

> **MANDATORY — BLOCKING STEP — DO NOT SKIP**
>
> **Note:** Docker containers run on the remote server (beast), not locally.
> All `docker` and `localhost` commands must be run via SSH, or test via Cloudflare tunnel URLs.
> Deploying code to the container is done exclusively through git push → CI/CD pipeline.

### Step 4.0: Pre-flight (local)

```bash
# Compile-check all modified Python files
cd agent-api && python -m py_compile main.py
```

### Step 4.1: Deploy via CI/CD

> **STOP — Ask user for approval before pushing**

Push branch and merge to `main` to trigger CI/CD deployment:
```bash
git push -u origin <branch-name>
# Create PR, merge to main → CI auto-deploys into container
```

### Step 4.2: API testing (via Cloudflare tunnel)

After CI completes, test from your local machine:

```bash
# Health check via Cloudflare
curl -s "https://secondbrain.yingliu.site/health" \
  -H "CF-Access-Client-Id: $CF_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET"
# Expected: {"status": "ok", "llm": {...}}

# Unauthenticated request is blocked
curl -s -o /dev/null -w "%{http_code}" "https://secondbrain.yingliu.site/health"
# Expected: 403
```

### Step 4.3: Container health (via CI logs)

Check the GitHub Actions run output for:
- Health check passed after deploy
- No container crash logs
- Container restarted successfully

```bash
# View latest CI run status
gh run list --workflow=deploy_agent_api.yml --limit 1
gh run view --log <run-id>
```

**After CI deploy succeeds and health check passes**, continue to Step 5.

---

## Step 5: Update Documentation

> **MANDATORY — BLOCKING STEP — DO NOT SKIP**

### 1. CHANGELOG.md

Add an entry at the top:
```markdown
## <version> - <Day Month Year>

- <brief feature summary>: <Title>
```

### 2. .claude/PROJECT_STRUCTURE.md

If changes affect project architecture (new files, new endpoints, new tools, new containers), update this file.

### 3. README.md

If changes affect setup, usage, environment variables, or deployment, update accordingly.

### 4. Deployment pipeline

If you added dependencies or changed container configuration, ensure these are reflected in:
- `docker-compose.yml` (container command / pip install line)
- `.github/workflows/deploy_agent_api.yml` (deploy step pip install)

---

## Step 6: Developer Review (Optional)

Ask whether the user wants to review changes before committing:

**Question:** "Would you like to review the diff before committing?"

If yes, show:
```bash
git diff --stat
git diff
```

---

## Step 7: Commit Changes

### Commit message format: `type: description`

| Type | Purpose | Example |
|------|---------|---------|
| `feat:` | New feature | `feat: add web search tool` |
| `fix:` | Bug fix | `fix: SSE timeout on long responses` |
| `security:` | Security change | `security: add per-session rate limiting` |
| `refactor:` | Code restructuring | `refactor: extract tool registry` |
| `docs:` | Documentation | `docs: update API endpoint docs` |
| `chore:` | Config/tooling | `chore: update deploy workflow` |

### Commit rules

1. **Stage specific files** — Use `git add <file>` for each file (avoid `git add .`)
2. **Imperative mood** — "add" not "added", "fix" not "fixed"
3. **Under 72 characters**
4. **No trailing period**
5. **Never commit** `.env`, `models/`, or `agent-api/data/`

---

## Step 8: Push Branch and Create Pull Request

### Push

```bash
git push -u origin <branch-name>
```

### Create PR

Use `gh pr create` or create via GitHub web UI.

**PR description** must include:
1. **Summary of changes**
2. **Which container(s) affected** and whether restart is needed
3. **Testing notes** — curl commands to verify
4. **Acceptance criteria** — confirm all met from Step 1

---

## Pre-PR Checklist

Before creating the pull request, verify **every item**:

- [ ] All acceptance criteria from Step 1 are met
- [ ] Python files compile (`python -m py_compile`)
- [ ] Local deploy test passed (Step 4.1)
- [ ] API endpoints tested (Step 4.2)
- [ ] User approved staging deploy (Step 4.3)
- [ ] Container health verified (Step 4.4)
- [ ] No secrets in committed code
- [ ] `.env` is in `.gitignore` and chmod 600
- [ ] New dependencies added to both docker-compose.yml and CI workflow
- [ ] SSE endpoints use POST (not GET)
- [ ] Auth middleware (`verify`) on all non-health endpoints
- [ ] Documentation updated
- [ ] Commit messages follow `type: description` format
- [ ] Branch name follows `type/brief-description` convention