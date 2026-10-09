import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.skills.registry import SkillRegistry
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

# CORS — the guest avatar profile's origins plus every tenant's registered
# origins. Outside the guest route (auth: origin, avatar_control only) the
# Origin header never grants access: tenant identity always comes from the
# per-tenant API key (Bearer + HMAC).
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(tenant_registry.guest().origins)
                         | set(tenant_registry.all_origins())),
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Timestamp", "X-Signature"],
)

# Host skills (the harness's host side). MCP tools are the mcp-gateway's:
# the guest calls it over :5011 (tenants.json `mcp_servers` is ignored here).
registry = SkillRegistry()

# Inject dependencies into routes
chat.set_registry(registry)
guest.set_tenant(tenant_registry.guest())
skills.set_registry(registry)
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
async def _start_temporal():
    # M5 (harness/docs/M5-CONTRACTS.md): TEMPORAL_ADDRESS set -> the session
    # workflow's worker runs on this loop with the real activities, and the
    # tenants' schedules are ensured, in the background: startup never waits
    # for Temporal, the direct path serves /chat until (unless) it is up.
    from app.host import temporal
    from app.host.activities import HostActivities
    acts = HostActivities(chat.workers, tenant_registry, registry)
    temporal.start_in_background(acts.activities(), tenant_registry.tenants_doc())


@app.on_event("shutdown")
async def _stop_temporal():
    from app.host import temporal
    await temporal.stop()


@app.on_event("shutdown")
async def _stop_harness_workers():
    # Stop every session's sandbox, LLM proxy and Unix-socket listeners.
    await chat.workers.shutdown()
    from app.gateway.client import aclose_clients
    await aclose_clients()
