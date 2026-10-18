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
│ • SwiftData      │                  │ vLLM                      │
│   (conversations)│                  │   Qwen3-14B-AWQ           │
└──────────────────┘                  └──────────────────────────┘

Pose Spatial Studio              SecondBrain (Server)
┌──────────────────┐              ┌──────────────────────────┐
│ robot.yingliu.site│ ── HTTPS ──►│ /api/v1/guest/chat       │
│ • Chat Panel     │  ◄── SSE ── │   avatar_control skill   │
│ • Voice Input    │              │   (unauthenticated)      │
└──────────────────┘              └──────────────────────────┘
```

## Tech Stack

| Layer | Technology |
|---|---|
| LLM Inference | vLLM (CUDA), Qwen3-14B-AWQ |
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
| `/api/v1/guest/chat` | POST | Origin check | Guest chat for avatar control (SSE streaming) |

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
| `avatar_control` | Server | Set pose, move joints, animate sequences for 3D avatar |

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

### End-to-end test in dev (`scripts/test-branch.sh`)

Runs any branch next to prod on the server and exposes it to the IndexApp on
your phone, so a branch can be tested end to end before it is merged. Prod
(`secondbrain-agent-api`, `secondbrain.yingliu.site`, its data and keys) is
never touched.

```sh
scripts/test-branch.sh <branch> [tenants.json]              # guest harness as a subprocess
scripts/test-branch-firecracker.sh <branch> [tenants.json]  # guest harness in Firecracker microVMs
scripts/test-branch.sh --down                               # remove leftover test containers
```

What a run does:

1. Checks out `origin/<branch>` in a separate worktree, `../SecondBrain-test`.
2. Builds the branch's agent-api image (`secondbrain-agent-api:test`) and, if the
   branch has `mcp-gateway/`, the gateway image.
3. Prepares a test data dir, `~/data/SecondBrain-test/agent-api/data`:
   `tenants.json` is the given file (default: prod's
   `~/data/SecondBrain/agent-api/data/tenants.json`), copied fresh on every run.
4. Starts a test MCP gateway (`secondbrain-test-agent-api-mcp-gateway`). Its
   `gateway.json` is generated from the test `tenants.json`, and each tenant gets
   only its own upstreams. The `default` tenant gets every upstream. It reaches wcc-mcp
   over `agent-net` and prints whether each upstream is reachable.
5. Starts agent-api (`secondbrain-test-agent-api`) on `127.0.0.1:8001`, sharing
   prod's LLM (`secondbrain-llm`). There is no Temporal, so it uses the direct
   path.
6. Runs a smoke test: one signed chat as the `default` tenant. It prints the
   reply, the tenant's tools by side, and, with Firecracker, the VM restore time.
7. Opens a Cloudflare quick tunnel and checks `/health` through it. It prints a
   `https://….trycloudflare.com` URL, then follows the logs. Ctrl-C stops
   everything.

`test-branch-firecracker.sh` first builds the guest VM image
(`harness/image`: `make image && make check`) and the base snapshot
(`base_snapshot build-base`, in a one-off container of the branch image), into
`~/data/SecondBrain-test/vm`. Both steps are skipped when they are up to date.
It then runs `test-branch.sh` with `SANDBOX=firecracker`.

**On the phone** (IndexApp → Settings), press Return after each field; a field
is saved only on Return:

- **Server URL**: the printed trycloudflare URL. It changes on every run.
- **API Key**: the test `default` tenant's key, from
  `cat ~/data/SecondBrain-test/default.key`. The key is generated once and is not
  prod's `API_SECRET_KEY`. A tenant `api_key` from the tenants file works too.
- **Test Connection** checks `/health` only. Send a message to check the key.
- Afterwards, set the Server URL back to `https://secondbrain.yingliu.site` and
  the API Key back to your prod key.

**Prerequisites** (the server): the prod compose stack up (networks
`secondbrain_tunnel_network` and `secondbrain_llm_internal`, the `secondbrain-llm`
container), the `agent-net` network with `wcc-mcp` for MCP, `cloudflared`, and,
for Firecracker, `/dev/kvm`.

**Options** (environment variables):

| Variable | Effect |
|---|---|
| `PORT=8001` | host port of the test agent-api |
| `FRESH=1` | wipe the test sessions before starting |
| `GATEWAY=0` | no mcp-gateway, so no MCP tools |
| `SMOKE=0` / `SMOKE_MSG="..."` | skip the smoke test / change its message, e.g. `"Use search_tools to find the WCC events tools"` |
| `NO_TUNNEL=1` | stop after the smoke test (a local check, no public URL) |
| `API_SECRET_KEY=...` | the default tenant's key, instead of `default.key` |
| `NAME=... DATA=...` | container name / data dir, to run a second test server alongside |
| `REBUILD_IMAGE=1` | (Firecracker) force a new guest image and base snapshot |

**Where to look:** the agent-api logs stream in the terminal; for the gateway
run `docker logs -f secondbrain-test-agent-api-mcp-gateway`; on the phone, use
Settings → Debug Logs. Each session is a directory
`~/data/SecondBrain-test/agent-api/data/sessions/{user}/{tenant}.{session}/`.
`events/r*/000001.jsonl` holds the guest events (`turn_start`, `tool_call`,
`tool_result`, `settle`), `events/host-*.jsonl` the host events (`vm`,
`gateway`, `usage`), and with Firecracker `vm/console.log` the guest console.

Not covered: the Temporal session workflow, and parking a session after
`HARNESS_SESSION_IDLE_S`.
