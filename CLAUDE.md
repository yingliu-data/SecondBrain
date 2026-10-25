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
- LLM vendor and local chat: `agent-api/data/config.json` holds the vendor (vllm, openai, anthropic, gemini) and any setting by its env name (see [config.json.example](agent-api/config.json.example); env vars win; restart to apply); terminal chat on the local sandbox: `cd agent-api && uv run python -m app.cli [--session NAME]` ([app/cli.py](agent-api/app/cli.py))
- Agent engine (host half): [agent-api/app/host/worker.py](agent-api/app/host/worker.py) drives one microVM guest per session; the guest harness (stdlib, event-sourced) is [harness/guest/](harness/guest/), the shared wire/event protocol [harness/protocol/](harness/protocol/), design and runbooks in [harness/docs/](harness/docs/)
- Auth middleware: [agent-api/app/auth/middleware.py](agent-api/app/auth/middleware.py)
- Skills folder: [agent-api/app/skills/](agent-api/app/skills/)
- AST index of projects and documents in MongoDB Atlas: the [ast-everything/](ast-everything/) library (Python and Markdown parsers, indexer, search), its agent tools in `ast_everything/tools.py` (usable by any Python agent), offered to SecondBrain's agents as host tools by the `ast_index` skill ([agent-api/app/skills/ast_index/](agent-api/app/skills/ast_index/)); credentials `MONGODB_*` in the repo `.env` (see [ast-everything/README.md](ast-everything/README.md))
- Tenants ("entries", per-API-key toolsets): [agent-api/app/tenants/](agent-api/app/tenants/) — config `data/tenants.json` (see [tenants.json.example](agent-api/tenants.json.example)), restart to apply; its `mcp_servers` lists are ignored by agent-api (MCP access lives in the gateway's `gateway.json`, generated with `cd mcp-gateway && uv run python -m app.migrate_config ../agent-api/data/tenants.json > data/gateway.json`)
- MCP: the [mcp-gateway/](mcp-gateway/) service (own container, config `mcp-gateway/data/gateway.json` — see [gateway.json.example](mcp-gateway/gateway.json.example), restart to apply) fronts every upstream MCP server; guests reach it through [agent-api/app/host/gateway_forwarder.py](agent-api/app/host/gateway_forwarder.py) — first server: wcc-pipeline `workspace/mcp_server/`
- Temporal session workflows and schedules: [flows/](flows/) (activities in [agent-api/app/host/activities.py](agent-api/app/host/activities.py))
- Tests: one uv workspace (root `pyproject.toml` + the one `uv.lock`; members `harness`, `flows`, `agent-api`, `mcp-gateway`, `ast-everything`): `uv sync --all-packages` at the root, then `uv run pytest -n auto` in each member dir (`harness/`, `flows/`, `agent-api/`, `mcp-gateway/`, `ast-everything/`) — separate pytest runs, as in CI ([.github/workflows/test.yml](.github/workflows/test.yml)); after a dependency change `uv lock` at the root
- Replay / verify session event logs: `cd harness && uv run python -m protocol.replay verify <session_dir>` (see [harness/docs/REPLAY.md](harness/docs/REPLAY.md))
- CI/CD: [.github/workflows/deploy_agent_api.yml](.github/workflows/deploy_agent_api.yml), [.github/workflows/build_mcp_gateway.yml](.github/workflows/build_mcp_gateway.yml) (images build from the repo root)
- Docker: [docker-compose.yml](docker-compose.yml)
