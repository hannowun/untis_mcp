# Multi-stage build (ADR-008): the builder keeps uv and the build tooling out of the image
# that runs on the Raspberry Pi, where every layer costs.
#
# The configuration is never baked in — it is mounted at runtime (NFR-07), and
# .dockerignore excludes config/ so a stray profiles.toml cannot slip into a layer.

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, without the project itself: this layer only changes when the lock file
# does, so editing the server does not reinstall httpx and fastmcp.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY untis_mcp ./untis_mcp
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim AS runtime

# tzdata is not optional here: without it the container thinks it is on UTC, and a server
# that answers "what does my daughter have today?" must agree with the school about which
# day it is (section 6.4).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 untis

WORKDIR /app

COPY --from=builder --chown=untis:untis /app/.venv /app/.venv
COPY --chown=untis:untis untis_mcp ./untis_mcp

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin \
    UNTIS_MCP_CONFIG=/app/config/profiles.toml \
    UNTIS_MCP_TRANSPORT=http \
    UNTIS_MCP_HOST=0.0.0.0 \
    UNTIS_MCP_PORT=8000 \
    UNTIS_MCP_LOG_LEVEL=INFO

USER untis
EXPOSE 8000

# The MCP endpoint needs a session handshake, so "is it listening" is the honest check.
#
# The start period is generous on purpose. Importing the server takes seconds on a developer
# machine but much longer on modest hardware — running this very image under emulation took
# over a minute, and a 10s start period turned a container that was merely still starting
# into a reported failure. A false alarm during startup is worse than a late true one.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ['UNTIS_MCP_PORT'])), 5).close()"]

CMD ["untis-mcp"]
