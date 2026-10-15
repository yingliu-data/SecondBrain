# syntax=docker/dockerfile:1
# agent-api image. Build context = the REPO ROOT (one uv workspace: root
# pyproject.toml + uv.lock; members harness/, flows/, agent-api/,
# mcp-gateway/):
#   docker build -f agent-api/Dockerfile .
# docker-compose.yml and CI set context/dockerfile accordingly;
# agent-api/Dockerfile.dockerignore keeps the context small.

# -- Firecracker (HARNESS_SANDBOX=firecracker) ------------------------------
# The pinned release tarball from harness/image/versions.env (the one pin
# for the whole repo: FIRECRACKER_VERSION/_URL/_SHA256), sha256-verified.
# Only the two static binaries leave this stage.
FROM python:3.12-slim AS firecracker
COPY harness/image/versions.env /tmp/versions.env
RUN set -eu; . /tmp/versions.env; cd /tmp \
 && python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], "fc.tgz")' "$FIRECRACKER_URL" \
 && echo "$FIRECRACKER_SHA256  fc.tgz" | sha256sum -c - \
 && tar -xzf fc.tgz \
 && d="release-$FIRECRACKER_VERSION-x86_64" \
 && install -m 0755 "$d/firecracker-$FIRECRACKER_VERSION-x86_64" /usr/local/bin/firecracker \
 && install -m 0755 "$d/jailer-$FIRECRACKER_VERSION-x86_64" /usr/local/bin/jailer \
 && /usr/local/bin/firecracker --version | grep -qF "$FIRECRACKER_VERSION"

FROM python:3.12-slim
# mkfs.ext4 formats each session's workspace.img (sandbox/firecracker.py;
# without it the sandbox falls back to HARNESS_WORKSPACE_TEMPLATE).
RUN apt-get update \
 && apt-get install -y --no-install-recommends e2fsprogs \
 && rm -rf /var/lib/apt/lists/*
COPY --from=firecracker /usr/local/bin/firecracker /usr/local/bin/jailer /usr/local/bin/
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# The workspace laid out as in the repo, rooted at /root: the venv is
# /root/.venv, the editable members are /root/harness and /root/flows.
# /root/harness is also HARNESS_GUEST_ROOT (the local sandbox runs
# `python -m guest.main` from it).
WORKDIR /root

# Dependency layer first so code-only changes reuse the cache. The frozen
# lock needs every member's pyproject.toml (mcp-gateway's too), not its code.
COPY pyproject.toml uv.lock ./
COPY harness/pyproject.toml harness/
COPY flows/pyproject.toml flows/
COPY agent-api/pyproject.toml agent-api/
COPY mcp-gateway/pyproject.toml mcp-gateway/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package agent-api --no-install-workspace

COPY harness/protocol harness/protocol
COPY harness/guest harness/guest
COPY flows/flows flows/flows
COPY agent-api/app agent-api/app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package agent-api

# Relative data paths (data/security.log, data/sessions) resolve under this
# workdir; docker-compose bind-mounts ./agent-api/data here.
WORKDIR /root/agent-api

ENV PATH="/root/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--http", "httptools", "--workers", "1"]
