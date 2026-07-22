FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# Relative data paths (data/security.log, data/sessions) resolve under this
# workdir; docker-compose bind-mounts ./agent-api/data here.
WORKDIR /root/agent-api

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer first so code-only changes reuse the cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

ENV PATH="/root/agent-api/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--http", "httptools", "--workers", "1"]
