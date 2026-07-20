import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.skills.registry import SkillRegistry
from app.agent.llm import LLMProvider
from app.auth import middleware
from app.tenants import create_tenant_registry
from app.routes import chat, tool_result, skills, health, sessions, guest

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

# Inject dependencies into routes
chat.set_dependencies(registry, llm)
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

# Start guest session cleanup task on startup
from app.auth.guest import guest_manager

@app.on_event("startup")
async def _start_guest_cleanup():
    guest_manager.start_cleanup()
