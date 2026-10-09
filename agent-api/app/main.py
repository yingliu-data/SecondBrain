import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.skills.registry import SkillRegistry
from app.agent.llm import LLMProvider
from app.auth import middleware
from app.tenants import create_tenant_registry
from app.routes import chat, tool_result, skills, health, sessions, guest
from app.gateway import events as gateway_events

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# Security events to file
sec_handler = logging.FileHandler("data/security.log")
sec_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger("security").addHandler(sec_handler)

# ── App ──────────────────────────────────────────────────────
app = FastAPI()

# Tenants ("entries"): per-API-key toolset/prompt/origins. Loaded once at
# startup from data/tenants.json; restart to apply changes.
tenant_registry = create_tenant_registry()
middleware.set_tenant_registry(tenant_registry)

# CORS — guest avatar origins plus every tenant's registered origins.
# The Origin header never grants access by itself: tenant identity always
# comes from the per-tenant API key (Bearer + HMAC).
GUEST_ORIGINS = [
    "https://robot.yingliu.site",
    "http://localhost:5173",
    "http://localhost:4173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(GUEST_ORIGINS) | set(tenant_registry.all_origins())),
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Timestamp", "X-Signature"],
)

# Initialize core components
registry = SkillRegistry()
llm = LLMProvider()

# Give skills access to LLM (for plan_movement decomposition)
registry.set_llm_provider(llm)

# Legacy engine: one skill per MCP upstream named in tenants.json
# `mcp_servers` (names only; URLs and tokens live in the gateway's
# gateway.json). Tool lists come from the mcp-gateway in the background
# after startup — a down gateway never blocks boot (M4-CONTRACTS §8).
from app.gateway.upstream_skill import GatewayUpstreamSkill

mcp_upstreams: list[GatewayUpstreamSkill] = []
for _name, _cfg in tenant_registry.mcp_server_configs().items():
    _skill = GatewayUpstreamSkill(_name, tool_defs_ttl_s=_cfg.tool_defs_ttl_s)
    registry.register(_skill)
    mcp_upstreams.append(_skill)

# Inject dependencies into routes
chat.set_dependencies(registry, llm)
# harness Workers: the tenant's answer_wait_s (a question park, either path)
from app.host.activities import worker_config
chat.workers.config_for = worker_config
guest.set_dependencies(registry, llm)
skills.set_registry(registry)
health.set_llm(llm)
sessions.set_sessions(chat.sessions)

# Mount routers (all under /api/v1/ for versioning, except /health)
app.include_router(chat.router)
app.include_router(tool_result.router)
app.include_router(skills.router)
app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(guest.router)
app.include_router(gateway_events.router)   # POST /internal/gateway-events (bearer INTERNAL_TOKEN)

# Start guest session cleanup task on startup
from app.auth.guest import guest_manager

@app.on_event("startup")
async def _start_guest_cleanup():
    guest_manager.start_cleanup()


@app.on_event("startup")
async def _start_mcp_refresh():
    import asyncio
    for skill in mcp_upstreams:
        asyncio.create_task(skill.start_background_refresh())


@app.on_event("startup")
async def _start_temporal():
    # M5 (harness/docs/M5-CONTRACTS.md): TEMPORAL_ADDRESS set -> the session
    # workflow's worker runs on this loop with the real activities, and the
    # tenants' schedules are ensured, in the background: startup never waits
    # for Temporal, the direct path serves /chat until (unless) it is up.
    from app.host import temporal
    from app.host.activities import HostActivities
    from app.session.dir_store import DirStore
    if not isinstance(chat.sessions, DirStore):
        return
    acts = HostActivities(chat.workers, tenant_registry, registry)
    temporal.start_in_background(acts.activities(), tenant_registry.tenants_doc())


@app.on_event("shutdown")
async def _stop_temporal():
    from app.host import temporal
    await temporal.stop()


@app.on_event("shutdown")
async def _stop_harness_workers():
    # Harness engine (AGENT_ENGINE=harness or a tenant's `engine`): stop every
    # session's sandbox, LLM proxy and Unix-socket listeners.
    await chat.workers.shutdown()
    from app.gateway.client import aclose_clients
    await aclose_clients()
