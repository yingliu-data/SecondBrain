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

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check (also checks LLM connectivity) |
| `/chat` | POST | Main conversation endpoint (SSE streaming) |
| `/tool_result` | POST | iPhone sends tool execution results back |

## GitHub Secrets Required

| Secret | Purpose |
|---|---|
| `SSH_PRIVATE_KEY` | SSH key for accessing the server |
| `SSH_HOSTNAME` | Cloudflare Access SSH hostname |
| `SSH_USER` | SSH username on the server |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token ID |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret |
