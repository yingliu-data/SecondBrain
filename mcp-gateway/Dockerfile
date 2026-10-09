# mcp-gateway image. Build context = the REPO ROOT (the harness package is a
# path dependency, `../harness`, for `protocol.canonical`):
#   docker build -f mcp-gateway/Dockerfile .
# docker-compose.yml sets context/dockerfile accordingly.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# The harness package (stdlib only) laid out as the lock expects: /srv/harness
# next to /srv/mcp-gateway. hatchling packages both protocol/ and guest/.
WORKDIR /srv
COPY harness/pyproject.toml harness/
COPY harness/protocol harness/protocol
COPY harness/guest harness/guest

# Dependency layer first so code-only changes reuse the cache.
WORKDIR /srv/mcp-gateway
COPY mcp-gateway/pyproject.toml mcp-gateway/uv.lock ./
RUN uv sync --frozen --no-dev

COPY mcp-gateway/app ./app

ENV PATH="/srv/mcp-gateway/.venv/bin:$PATH" \
    GATEWAY_CONFIG=/data/gateway.json \
    GATEWAY_STATE_DIR=/data/gateway-state
EXPOSE 8090
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
