# Project Structure

```
SecondBrain/
├── .github/
│   └── workflows/
│       └── deploy_agent_api.yml    # CI/CD: push to main → deploy into container
├── .claude/
│   ├── dev/
│   │   └── SKILL.md                # Development workflow skill
│   └── PROJECT_STRUCTURE.md        # This file
├── agent-api/
│   ├── main.py                     # FastAPI application (deployed into container)
│   └── data/                       # Persistent data — gitignored (logs, db)
├── models/                         # LLM model files — gitignored (~9 GB)
├── docker-compose.yml              # Defines llm, agent-api, cloudflared containers
├── .env.example                    # Template for required secrets
├── .gitignore
├── CHANGELOG.md
├── LICENSE
└── PLAN.md                         # 12-week build guide
```

## Containers

| Container | Image | Purpose | Restart on deploy? |
|---|---|---|---|
| `secondbrain-llm` | `ghcr.io/ggml-org/llama.cpp:server-cuda` | Qwen3 14B inference on GPU | NO |
| `secondbrain-agent-api` | `python:3.12-slim` | FastAPI agent loop | YES — code deployed via CI |
| `secondbrain-cloudflared` | `cloudflare/cloudflared:latest` | Tunnel networking | NO |

## Endpoints

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/health` | GET | None | Health check (also checks LLM connectivity) |
| `/chat` | POST | Bearer + Timestamp + HMAC | Main conversation endpoint (SSE streaming) |
| `/tool_result` | POST | Bearer + Timestamp + HMAC | iPhone sends tool execution results back |

## Auth Middleware (3-layer)

1. **Bearer token** — `Authorization: Bearer <API_SECRET_KEY>`
2. **Timestamp freshness** — `X-Timestamp` must be within 300s of server time
3. **HMAC signature** — `X-Signature` = HMAC-SHA256(API_SECRET_KEY, timestamp + body)

## Tool Definitions

Tools defined in agent-api, executed on iPhone: `get_calendar_events`, `create_calendar_event`, `get_reminders`, `create_reminder`, `search_contacts`, `read_clipboard`, `web_search`

## GitHub Secrets Required

| Secret | Purpose |
|---|---|
| `SSH_PRIVATE_KEY` | SSH key for accessing the server |
| `SSH_HOSTNAME` | Cloudflare Access SSH hostname |
| `SSH_USER` | SSH username on the server |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token ID |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret |
