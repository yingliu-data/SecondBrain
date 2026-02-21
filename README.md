# SecondBrain

Personal AI assistant — a home server runs the brain (LLM + agent logic) and an iPhone app acts as a thin client handling voice, text, and on-device tools (Calendar, Reminders, Contacts).

## Architecture

```
iPhone 15 Pro                          Server (RTX 5080)
┌──────────────────┐                   ┌──────────────────────────┐
│ SwiftUI App      │  ── HTTPS ──►     │ Cloudflare Tunnel        │
│ • Chat UI        │  (Cloudflare)     │   ↓                      │
│ • Voice (STT/TTS)│  ◄── SSE ──      │ Agent API (MCP Host)     │
│ • Tool Executors │                   │   ├── Skills system      │
│   (Calendar,     │                   │   │   (auto-discovery,   │
│    Reminders,    │                   │   │    CRUD management)   │
│    Contacts...)  │                   │   ├── Native fn calling   │
│                  │                   │   └── LLM provider       │
│                  │                   │       ↓                   │
│                  │                   │ llama-server              │
│                  │                   │   Qwen3 14B + 0.5B draft  │
└──────────────────┘                   └──────────────────────────┘
```

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check + LLM connectivity |
| `/api/v1/chat` | POST | Conversation with SSE streaming |
| `/api/v1/tool-result` | POST | iPhone sends tool results back |
| `/api/v1/skills` | GET | List all skills |
| `/api/v1/skills/{name}` | PATCH | Enable/disable a skill |

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

## iOS Client (Finger)

SwiftUI app source files are in `Finger/`. To use:

1. Create a new Xcode project (SwiftUI, iOS 26, name: "Finger")
2. Add the Swift files from `Finger/` into the project
3. Add Info.plist entries for Calendar, Reminders, and Contacts usage descriptions
4. Replace placeholder credentials in `AssistantClient.swift` with your actual keys

## Development

See [PLAN.md](PLAN.md) for the full build guide.
