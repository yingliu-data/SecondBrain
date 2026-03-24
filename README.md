# SecondBrain

Personal AI assistant — a home server runs the brain (LLM + agent logic) and an iPhone app acts as a thin client handling voice, text, and on-device tools (Calendar, Reminders, Contacts).

## Repos

| Repo | Contents |
|---|---|
| [SecondBrain](https://github.com/yingliu-data/SecondBrain) | Backend: FastAPI agent API, Docker orchestration, CI/CD |
| [IndexApp](https://github.com/yingliu-data/IndexApp) | iOS client: SwiftUI app (iOS 26, Liquid Glass) |

## Architecture

```
IndexApp (iPhone)                    SecondBrain (Server, RTX 5080)
┌──────────────────┐                  ┌──────────────────────────┐
│ SwiftUI App      │  ── HTTPS ──►    │ Cloudflare Tunnel        │
│ • Chat UI        │  (Cloudflare)    │   ↓                      │
│ • Voice (WK STT) │  ◄── SSE ──     │ Agent API (FastAPI)      │
│ • Markdown render│                  │   ├── Skills system      │
│ • Tool Executors │                  │   │   (auto-discovery,   │
│   (Calendar,     │                  │   │    CRUD management)   │
│    Reminders,    │                  │   ├── Native fn calling   │
│    Contacts,     │                  │   └── LLM provider       │
│    Clipboard)    │                  │       ↓                   │
│ • SwiftData      │                  │ llama-server              │
│   (conversations)│                  │   Qwen3 14B + 0.5B draft │
└──────────────────┘                  └──────────────────────────┘
```

## Tech Stack

| Layer | Technology |
|---|---|
| LLM Inference | llama.cpp (CUDA), Qwen3 14B q4_k_m + 0.5B draft |
| Backend | Python 3.12, FastAPI, Uvicorn, SSE-Starlette |
| iOS Client | Swift, SwiftUI, SwiftData, WhisperKit (STT), ElevenLabs (TTS) |
| Auth | HMAC-SHA256 + Bearer token + Timestamp verification |
| Networking | Cloudflare Tunnel (Zero Trust) |
| Infrastructure | Docker Compose (3 containers), GitHub Actions CI/CD |

## API Endpoints

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/health` | GET | None | Health check + LLM connectivity |
| `/api/v1/chat` | POST | Bearer + HMAC | Conversation with SSE streaming |
| `/api/v1/tool-result` | POST | Bearer + HMAC | iPhone sends tool results back |
| `/api/v1/skills` | GET | Bearer + HMAC | List all skills with metadata |
| `/api/v1/skills/{name}` | GET | Bearer + HMAC | Get details for a specific skill |
| `/api/v1/skills/{name}` | PATCH | Bearer + HMAC | Enable/disable a skill |

## Skills

Skills are modular capabilities under `agent-api/app/skills/`. Each extends `BaseSkill` and is auto-discovered at startup.

| Skill | Execution | Description |
|---|---|---|
| `web_search` | Server | DuckDuckGo web search |
| `github_cli` | Server | GitHub CLI operations |
| `gitlab_cli` | Server | GitLab CLI operations |
| `calendar` | Device | Get/create calendar events (EventKit) |
| `reminders` | Device | Get/create reminders (EventKit) |
| `contacts` | Device | Search contacts by name |
| `clipboard` | Device | Read iPhone clipboard |

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

## iOS Client (Index)

The iOS app lives in the separate [IndexApp](https://github.com/yingliu-data/IndexApp) repo. Open `Index.xcodeproj` in Xcode.

**Key features:**
- Multi-conversation chat with streaming markdown rendering
- Live voice transcription via WhisperKit on-device STT
- ElevenLabs TTS for natural assistant voice output
- Device tool executors (Calendar, Reminders, Contacts, Clipboard)
- Skill management with device/server badges and LLM-powered Add New Skill
- Dark-first UI with glass-morphism / Liquid Glass aesthetic
- SwiftData persistence for conversations and messages
- Debug logs viewer in Settings

**Privacy descriptions required** (in build settings):
- `NSCalendarsUsageDescription`
- `NSRemindersUsageDescription`
- `NSContactsUsageDescription`
- `NSMicrophoneUsageDescription`

## Development

See [PLAN.md](PLAN.md) for the full build guide and `.claude/PROJECT_STRUCTURE.md` for detailed file-level documentation.
