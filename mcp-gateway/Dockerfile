# syntax=docker/dockerfile:1
# mcp-gateway image. Build context = the REPO ROOT (one uv workspace: root
# pyproject.toml + uv.lock; the gateway uses the harness member's
# protocol.canonical / protocol.mcp):
#   docker build -f mcp-gateway/Dockerfile .
# docker-compose.yml and CI set context/dockerfile accordingly;
# mcp-gateway/Dockerfile.dockerignore keeps the context small.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# The workspace laid out as in the repo, rooted at /srv: the venv is
# /srv/.venv, the harness member is installed editable from /srv/harness.
WORKDIR /srv

# Dependency layer first so code-only changes reuse the cache. The frozen
# lock needs every member's pyproject.toml, not their code.
COPY pyproject.toml uv.lock ./
COPY harness/pyproject.toml harness/
COPY flows/pyproject.toml flows/
COPY agent-api/pyproject.toml agent-api/
COPY mcp-gateway/pyproject.toml mcp-gateway/
COPY ast-everything/pyproject.toml ast-everything/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package mcp-gateway --no-install-workspace

# Only protocol/: the gateway never imports guest/, and the editable install
# of the harness member is a .pth to /srv/harness, so hatchling does not
# need guest/ to be present.
COPY harness/protocol harness/protocol
COPY mcp-gateway/app mcp-gateway/app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package mcp-gateway

WORKDIR /srv/mcp-gateway
ENV PATH="/srv/.venv/bin:$PATH" \
    GATEWAY_CONFIG=/data/gateway.json \
    GATEWAY_STATE_DIR=/data/gateway-state
EXPOSE 8090
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
