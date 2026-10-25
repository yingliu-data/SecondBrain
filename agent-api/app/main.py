import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.skills.registry import SkillRegistry
from app.auth import middleware
from app.auth.guest import guest_manager
from app.tenants import create_tenant_registry
from app.routes import chat, tool_result, skills, health, sessions, guest
from app.gateway import events as gateway_events
from app.config import MEMORY_INDEX

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
    allow_origins=tenant_registry.all_origins(),
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
app.include_router(gateway_events.router)   # POST GATEWAY_EVENTS_PATH (bearer INTERNAL_TOKEN)

# Start guest session cleanup task on startup
@app.on_event("startup")
async def _start_guest_cleanup():
    guest_manager.start_cleanup()


@app.on_event("startup")
async def _warn_legacy_layout():
    # Old-layout session directories ({user}/{tenant}_{session_id}) are
    # ignored; one WARNING names them (nothing is deleted automatically).
    import asyncio
    from app.session.dir_store import warn_legacy_layout
    try:
        await asyncio.to_thread(warn_legacy_layout, chat.sessions.root,
                                logging.getLogger("startup"))
    except Exception:  # noqa: BLE001 - never block startup
        logging.getLogger("startup").exception("scanning for old-layout sessions failed")


@app.on_event("startup")
async def _resume_question_parks():
    # Sessions parked on a question of the direct path before a restart:
    # their answer wait goes on (vm/parked.json holds its deadline).
    try:
        await chat.workers.load_question_parks(tenant_registry.all_tenants(), registry)
    except Exception:  # noqa: BLE001 - never block startup
        logging.getLogger("startup").exception("restoring question parks failed")


@app.on_event("startup")
async def _start_temporal():
    # TEMPORAL_ADDRESS set -> every input goes through the session workflow:
    # its worker runs on this loop with the real activities (a supervisor
    # reconnects / restarts it), and the tenants' schedules are ensured, in
    # the background: startup never waits for Temporal; /chat answers 503
    # (Retry-After) while it is not ready. Unset -> the direct path only.
    from app.host import temporal
    from app.host.activities import HostActivities
    acts = HostActivities(chat.workers, tenant_registry, registry)
    temporal.start_in_background(acts.activities(), tenant_registry.tenants_doc())


@app.on_event("shutdown")
async def _stop_temporal():
    from app.host import temporal
    await temporal.stop()


_memory_index_task = None
_memory_indexer = None


@app.on_event("startup")
async def _start_memory_index():
    # MEMORY_INDEX: a closed or idle session and a memory file written go into the memory graph,
    # and each input recalls from it
    global _memory_index_task, _memory_indexer
    if MEMORY_INDEX:
        import asyncio
        from app.host import memory_index
        _memory_indexer = memory_index.MemoryIndexer(chat.sessions, tenant_registry.all_tenants())
        memory_index.activate(_memory_indexer)
        chat.workers.on_closed = _memory_indexer.closed
        _memory_index_task = asyncio.get_running_loop().create_task(_memory_indexer.run(), name="memory-index")


@app.on_event("shutdown")
async def _stop_memory_index():
    # before _stop_harness_workers: the Workers it closes start no index run
    from app.host import memory_index
    memory_index.activate(None)
    chat.workers.on_closed = None
    if _memory_index_task is not None:
        _memory_index_task.cancel()
    if _memory_indexer is not None:
        _memory_indexer.close()


@app.on_event("shutdown")
async def _stop_harness_workers():
    # Park every running guest (HARNESS_SHUTDOWN_PARK_S: a restart restores
    # it and adopts its open device calls), then stop every session's
    # sandbox, LLM proxy and Unix-socket listeners.
    await chat.workers.shutdown()
    from app.gateway.client import aclose_clients
    await aclose_clients()
