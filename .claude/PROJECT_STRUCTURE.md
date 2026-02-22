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
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI entry point, mounts routers
│   │   ├── config.py               # Environment config, constants, system prompt
│   │   ├── auth/
│   │   │   ├── __init__.py
│   │   │   └── middleware.py       # Bearer + HMAC + timestamp verification
│   │   ├── agent/
│   │   │   ├── __init__.py
│   │   │   ├── loop.py             # Core agent loop (LLM ↔ tool orchestration)
│   │   │   ├── llm.py              # LLM provider abstraction (local + cloud fallback)
│   │   │   └── sanitize.py         # Prompt injection defense
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── chat.py             # POST /api/v1/chat (SSE streaming)
│   │   │   ├── tool_result.py      # POST /api/v1/tool-result
│   │   │   ├── skills.py           # GET/PATCH /api/v1/skills CRUD
│   │   │   └── health.py           # GET /health
│   │   └── skills/                 # ← Drop-in skills folder
│   │       ├── __init__.py
│   │       ├── base.py             # BaseSkill abstract class
│   │       ├── registry.py         # Auto-discovers + manages skills
│   │       ├── web_search/         # Server-side skill (DuckDuckGo)
│   │       ├── calendar/           # Device-side skill (iPhone EventKit)
│   │       ├── reminders/          # Device-side skill (iPhone EventKit)
│   │       ├── contacts/           # Device-side skill (iPhone Contacts)
│   │       └── clipboard/          # Device-side skill (iPhone UIPasteboard)
│   ├── requirements.txt            # Python dependencies
│   └── data/                       # Persistent data — gitignored (logs, db, skills.json)
├── models/                         # LLM model files — gitignored (~9 GB)
├── FingerApp/                       # iOS SwiftUI app (iOS 26, Liquid Glass)
│   ├── FingerApp.swift              # App entry point → MainTabView + SwiftData container
│   ├── Assets.xcassets/             # App icon, wallpaper image
│   ├── Models/
│   │   ├── Conversation.swift       # SwiftData @Model for chat conversations
│   │   └── Message.swift            # SwiftData @Model for messages (relationship to Conversation)
│   ├── Theme/
│   │   └── Theme.swift              # AppTheme colors, gradients, radii
│   ├── Network/
│   │   ├── AssistantClient.swift    # HMAC auth, SSE parsing, tool call routing
│   │   └── DebugLog.swift           # Timestamped request/response logging
│   ├── Tools/
│   │   ├── CalendarTool.swift       # EventKit: get/create calendar events
│   │   ├── RemindersTool.swift      # EventKit: get/create reminders
│   │   └── ContactsTool.swift       # Contacts: search by name
│   ├── Voice/
│   │   └── SpeechManager.swift      # On-device speech recognition (SFSpeechRecognizer + AVAudioEngine)
│   └── Views/
│       ├── MainTabView.swift        # Native TabView + NavigationStack for chat
│       ├── ConversationListView.swift # Conversation list with create/delete/switch
│       ├── ChatView.swift           # Chat with markdown, choice buttons, image upload
│       ├── ChoiceButtonsView.swift  # Tappable Liquid Glass buttons for LLM options
│       ├── SkillsView.swift         # Skill toggle cards with device/server badges
│       └── SettingsView.swift       # Preferences and about
├── docker-compose.yml              # Defines llm, agent-api, cloudflared containers
├── .env.example                    # Template for required secrets
├── .gitignore
├── CHANGELOG.md
├── LICENSE
└── PLAN.md                         # Build guide
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
| `/api/v1/chat` | POST | Bearer + Timestamp + HMAC | Main conversation endpoint (SSE streaming) |
| `/api/v1/tool-result` | POST | Bearer + Timestamp + HMAC | iPhone sends tool execution results back |
| `/api/v1/skills` | GET | Bearer + Timestamp + HMAC | List all skills with metadata and enabled state |
| `/api/v1/skills/{name}` | GET | Bearer + Timestamp + HMAC | Get details for a specific skill |
| `/api/v1/skills/{name}` | PATCH | Bearer + Timestamp + HMAC | Enable or disable a skill |

## Auth Middleware (3-layer)

1. **Bearer token** — `Authorization: Bearer <API_SECRET_KEY>`
2. **Timestamp freshness** — `X-Timestamp` must be within 300s of server time
3. **HMAC signature** — `X-Signature` = HMAC-SHA256(API_SECRET_KEY, timestamp + body)

## Skills System

Skills are self-contained capabilities under `app/skills/`. Each skill extends `BaseSkill` and defines tool definitions, execution side (server or device), and keywords for query-based filtering.

| Skill | Execution | Tools |
|---|---|---|
| `web_search` | Server | `web_search` |
| `calendar` | Device | `get_calendar_events`, `create_calendar_event` |
| `reminders` | Device | `get_reminders`, `create_reminder` |
| `contacts` | Device | `search_contacts` |
| `clipboard` | Device | `read_clipboard` |

Adding a new skill: create a folder under `app/skills/`, add `skill.py` extending `BaseSkill`, restart.

## GitHub Secrets Required

| Secret | Purpose |
|---|---|
| `SSH_PRIVATE_KEY` | SSH key for accessing the server |
| `SSH_HOSTNAME` | Cloudflare Access SSH hostname |
| `SSH_USER` | SSH username on the server |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token ID |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret |
