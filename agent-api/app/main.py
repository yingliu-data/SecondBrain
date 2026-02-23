import logging
from fastapi import FastAPI
from app.skills.registry import SkillRegistry
from app.agent.llm import LLMProvider
from app.routes import chat, tool_result, skills, health, sessions

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

# Initialize core components
registry = SkillRegistry()
llm = LLMProvider()

# Inject dependencies into routes
chat.set_dependencies(registry, llm)
skills.set_registry(registry)
health.set_llm(llm)
sessions.set_sessions(chat.sessions)

# Mount routers (all under /api/v1/ for versioning, except /health)
app.include_router(chat.router)
app.include_router(tool_result.router)
app.include_router(skills.router)
app.include_router(health.router)
app.include_router(sessions.router)
