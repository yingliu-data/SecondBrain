import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.skills.registry import SkillRegistry
from app.agent.llm import LLMProvider
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

# CORS — allow guest frontend origins for the unauthenticated avatar endpoint
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://robot.yingliu.site",
        "http://localhost:5173",
        "http://localhost:4173",
    ],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
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
