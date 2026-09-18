# SecondBrain — Agent Configuration

> **Agent documents live IN THIS REPO at `.claude/skills/second-brain/`.**
>
> A previous version of this file pointed at
> `/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/`. That directory
> exists but contains only `SecondBrain.code-workspace` — the three documents
> this table used to reference were never there, so every agent session that
> trusted this file began with a failed lookup. Corrected 17 Sept 2026.

## Agent Docs

| Document | Path | Purpose |
|----------|------|---------|
| Skill router | [.claude/skills/second-brain/SKILL.md](.claude/skills/second-brain/SKILL.md) | Entry point for the second-brain workflows |
| Project Structure | [.claude/skills/second-brain/PROJECT_STRUCTURE.md](.claude/skills/second-brain/PROJECT_STRUCTURE.md) | Architecture, containers, endpoints, skills. **Partly stale** — describes removed Gemini/llama.cpp design and presents unmerged multi-agent work as live |
| Development Workflow | [.claude/skills/second-brain/dev/SKILL.md](.claude/skills/second-brain/dev/SKILL.md) | Full dev workflow (`/develop`) |
| iOS Test Workflow | [.claude/skills/second-brain/ios-test/SKILL.md](.claude/skills/second-brain/ios-test/SKILL.md) | iOS test skill (`/ios-test`) |
| Server Debug | [.claude/skills/second-brain/server-debug/SKILL.md](.claude/skills/second-brain/server-debug/SKILL.md) | Server debugging. **Stale** — says llama.cpp; the stack is vLLM |
| VSCode Workspace | `/Users/sophia/Local Projects/.claude/SecondBrain-AGENT/SecondBrain.code-workspace` | Multi-root workspace. The one file that really is outside the repo |

## Current design of record

| Document | Purpose |
|----------|---------|
| [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) | v2 architecture: MCP gateway, deterministic tier, microVM deployment, Pi-derived gap register |
| [WORK_PLAN.md](WORK_PLAN.md) | Phased execution plan, with the argument against itself. **Read "The case against this plan" first** |
| [ANALYSIS.md](ANALYSIS.md) | 2026-06-13 multi-agent audit. Several findings since fixed — check before trusting a line |

## Repos

| Repo | Local Path | GitHub |
|------|-----------|--------|
| **SecondBrain** (backend) | `/Users/sophia/Local Projects/SecondBrain/` | `github.com/yingliu-data/SecondBrain` |
| **IndexApp** (iOS) | `/Users/sophia/Local Projects/IndexApp/` | `github.com/yingliu-data/IndexApp` |

## Quick Reference

- Backend entry point: [agent-api/app/main.py](agent-api/app/main.py)
- Config & system prompt: [agent-api/app/config.py](agent-api/app/config.py)
- Agent loop: [agent-api/app/agent/loop.py](agent-api/app/agent/loop.py)
- Auth middleware: [agent-api/app/auth/middleware.py](agent-api/app/auth/middleware.py)
- Skills folder: [agent-api/app/skills/](agent-api/app/skills/)
- Tenants ("entries", per-API-key toolsets): [agent-api/app/tenants/](agent-api/app/tenants/) — config `data/tenants.json` (see [tenants.json.example](agent-api/tenants.json.example)), restart to apply
- MCP client (remote MCP servers as skills): [agent-api/app/mcp/](agent-api/app/mcp/) — first server: wcc-pipeline `workspace/mcp_server/`
- Tests: `cd agent-api && uv sync && uv run pytest` (deps are uv-managed: `pyproject.toml` + `uv.lock`)
- CI/CD: [.github/workflows/deploy_agent_api.yml](.github/workflows/deploy_agent_api.yml)
- Docker: [docker-compose.yml](docker-compose.yml)
