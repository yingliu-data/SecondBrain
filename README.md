# SecondBrain

Personal AI assistant — a home server runs the brain (LLM + agent logic) and an iPhone app acts as a thin client handling voice, text, and on-device tools (Calendar, Reminders, Contacts).

## Architecture

```
iPhone 15 Pro                          Server (RTX 5080)
┌──────────────────┐                   ┌──────────────────────────┐
│ SwiftUI App      │  ── HTTPS ──►     │ Cloudflare Tunnel        │
│ • Chat UI        │  (Cloudflare)     │   ↓                      │
│ • Voice (STT/TTS)│  ◄── SSE ──      │ FastAPI (agent loop)     │
│ • Tool Executors │                   │   ↓                      │
│   (Calendar,     │                   │ llama-server             │
│    Reminders,    │                   │   Qwen3 14B + 0.5B draft │
│    Contacts...)  │                   │   ~50-80 tok/s           │
└──────────────────┘                   └──────────────────────────┘
```

## Deployment

Push to `main` triggers GitHub Actions which SSHs into the server via Cloudflare Tunnel and deploys the agent-api code into the running container.

### Required GitHub Secrets

| Secret | Purpose |
|---|---|
| `SSH_PRIVATE_KEY` | SSH key for server access |
| `SSH_HOSTNAME` | Cloudflare Access SSH hostname |
| `SSH_USER` | SSH username on the server |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token ID |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret |

### Required Server Environment (`.env`)

See `.env.example` for the template. Generate secrets with `openssl rand -hex 32`.

## Development

See [PLAN.md](PLAN.md) for the full 12-week build guide.
