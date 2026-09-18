FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# Relative data paths (data/security.log, data/sessions) resolve under this
# workdir; docker-compose bind-mounts ./agent-api/data here.
WORKDIR /root/agent-api

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Paths are repo-root-relative: the build context is the repo root (see
# deploy_agent_api.yml) so that packages/ is reachable.
# Dependency layer first so code-only changes reuse the cache. sb-contracts is
# copied before `uv sync` because it is a path dependency resolved at sync time.
COPY packages/sb-contracts /root/packages/sb-contracts
COPY agent-api/pyproject.toml agent-api/uv.lock ./
RUN uv sync --frozen --no-dev

COPY agent-api/app ./app

ENV PATH="/root/agent-api/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--http", "httptools", "--workers", "1"]
