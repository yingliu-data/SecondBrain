# SecondBrain — Agent Configuration

> **IMPORTANT: All agent documents (project structure, skills, workflows) are centralized at:**
>
> **`/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/`**
>
> Do NOT create or duplicate agent docs inside this repo's `.claude/` folder.

## Centralized Agent Docs

| Document | Path | Purpose |
|----------|------|---------|
| Project Structure | `/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/PROJECT_STRUCTURE.md` | Architecture, containers, endpoints, skills |
| Development Workflow | `/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/dev/SKILL.md` | Full dev workflow skill (`/develop`) |
| iOS Test Workflow | `/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/ios-test/SKILL.md` | iOS test skill (`/ios-test`) |
| VSCode Workspace | `/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/SecondBrain.code-workspace` | Multi-root workspace (SecondBrain + IndexApp + .claude) |

## Repos

| Repo | Local Path | GitHub |
|------|-----------|--------|
| **SecondBrain** (backend) | `/Users/sophia/Local Projects/SecondBrain/` | `github.com/yingliu-data/SecondBrain` |
| **IndexApp** (iOS) | `/Users/sophia/Local Projects/IndexApp/` | `github.com/yingliu-data/IndexApp` |

## Quick Reference

- Backend entry point: [agent-api/app/main.py](agent-api/app/main.py)
- Config & system prompt: [agent-api/app/config.py](agent-api/app/config.py)
- Agent engine (host half): [agent-api/app/host/worker.py](agent-api/app/host/worker.py) drives one microVM guest per session; the guest harness (stdlib, event-sourced) is [harness/guest/](harness/guest/), the shared wire/event protocol [harness/protocol/](harness/protocol/), design and runbooks in [harness/docs/](harness/docs/)
- Auth middleware: [agent-api/app/auth/middleware.py](agent-api/app/auth/middleware.py)
- Skills folder: [agent-api/app/skills/](agent-api/app/skills/)
- Tenants ("entries", per-API-key toolsets): [agent-api/app/tenants/](agent-api/app/tenants/) — config `data/tenants.json` (see [tenants.json.example](agent-api/tenants.json.example)), restart to apply
- MCP: the [mcp-gateway/](mcp-gateway/) service (own container, config `gateway.json`) fronts every upstream MCP server; guests reach it through [agent-api/app/host/gateway_forwarder.py](agent-api/app/host/gateway_forwarder.py) — first server: wcc-pipeline `workspace/mcp_server/`
- Temporal session workflows and schedules: [flows/](flows/) (activities in [agent-api/app/host/activities.py](agent-api/app/host/activities.py))
- Tests: `cd agent-api && uv sync && uv run pytest`; also `harness/`, `mcp-gateway/` and `flows/` each `uv run pytest` (deps are uv-managed: `pyproject.toml` + `uv.lock`)
- Replay / verify session event logs: `python -m protocol.replay verify <session_dir>` (see [harness/docs/REPLAY.md](harness/docs/REPLAY.md))
- CI/CD: [.github/workflows/deploy_agent_api.yml](.github/workflows/deploy_agent_api.yml), [.github/workflows/build_mcp_gateway.yml](.github/workflows/build_mcp_gateway.yml) (images build from the repo root)
- Docker: [docker-compose.yml](docker-compose.yml)
