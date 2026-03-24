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
- Agent loop: [agent-api/app/agent/loop.py](agent-api/app/agent/loop.py)
- Auth middleware: [agent-api/app/auth/middleware.py](agent-api/app/auth/middleware.py)
- Skills folder: [agent-api/app/skills/](agent-api/app/skills/)
- CI/CD: [.github/workflows/deploy_agent_api.yml](.github/workflows/deploy_agent_api.yml)
- Docker: [docker-compose.yml](docker-compose.yml)
