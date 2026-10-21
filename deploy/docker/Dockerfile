# syntax=docker/dockerfile:1.7
# Tidal image. This file currently holds the DEV stage only (T-112): it is what
# `docker compose --profile app up` builds. T-305 adds the hardened production stages
# (distroless/slim digest pin, read-only rootfs, healthcheck, size budget) to this same file.
# Build context is the repository root.

FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

# ---- builder: resolve and install locked dependencies into /app/.venv -----------------------
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first (layer cache survives source edits).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Then the project itself (README.md is referenced by pyproject; uv_build needs src/).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---- dev: runtime for docker compose ----------------------------------------------------------
FROM python:3.12-slim-bookworm AS dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# ODBC Driver 18 for SQL Server (pyodbc needs it to reach the sqlserver container).
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg unixodbc \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 tidal \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tidal
WORKDIR /app
COPY --from=builder --chown=10001:10001 /app /app
USER 10001:10001
EXPOSE 8000
CMD ["tidal-api"]
