# agent-api image. Build context = the REPO ROOT (secondbrain-harness and
# flows are editable path dependencies, `../harness` and `../flows`):
#   docker build -f agent-api/Dockerfile .
# docker-compose.yml and CI set context/dockerfile accordingly;
# agent-api/Dockerfile.dockerignore keeps the context small.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# The path deps laid out as the lock expects: /root/harness and /root/flows
# next to /root/agent-api. /root/harness is also HARNESS_GUEST_ROOT (the
# local sandbox runs `python -m guest.main` from it).
WORKDIR /root
COPY harness/pyproject.toml harness/
COPY harness/protocol harness/protocol
COPY harness/guest harness/guest
COPY flows/pyproject.toml flows/
COPY flows/flows flows/flows

# Relative data paths (data/security.log, data/sessions) resolve under this
# workdir; docker-compose bind-mounts ./agent-api/data here.
WORKDIR /root/agent-api

# Dependency layer first so code-only changes reuse the cache.
COPY agent-api/pyproject.toml agent-api/uv.lock ./
RUN uv sync --frozen --no-dev

COPY agent-api/app ./app

ENV PATH="/root/agent-api/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--http", "httptools", "--workers", "1"]
