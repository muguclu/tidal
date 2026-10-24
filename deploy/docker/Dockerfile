# syntax=docker/dockerfile:1.7
# Tidal image (T-112 dev, T-305 prod). One image, many entrypoints: the deploy manifest picks
# the command (tidal-api | tidal-query | tidal-worker | tidal-tiering | tidal ...).
# Stages: uv -> builder -> runtime (ODBC 18 + UID 10001) -> dev | prod (default, last stage).
# Build context is the repository root. Base images are pinned by digest (SPEC-06 5); bump the
# digests deliberately (Dependabot/renovate), never via floating tags.
#
# TIDAL_ENV is intentionally NOT set here (ADR-0012: no default, fail closed). The same image is
# promoted dev -> prod, so baking an env would be wrong or dangerous; the Helm chart / compose /
# Container Apps definition must set TIDAL_ENV explicitly or the process refuses to start.
#
# HEALTHCHECK is intentionally omitted: Kubernetes ignores it, and the probe path differs per
# entrypoint (api /v1/health/live, query /health/live, worker none). Probes live in the manifests.
# Run with readOnlyRootFilesystem and a writable emptyDir/tmpfs on /tmp.

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

# ---- builder: resolve and install locked dependencies into /app/.venv -----------------------
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
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
# Migrations ship with the image so `tidal db migrate` works in containers (default --dir).
COPY sql ./sql
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Bake the DuckDB azure extension (signature checks stay ON: allow_unsigned_extensions is never set).
# Outside TIDAL_ENV=local the code only runs LOAD (no network, no home dir), so it must be here.
# Same directory the runtime reads via DUCKDB_EXTENSION_DIRECTORY (tidal.query.duck).
RUN /app/.venv/bin/python -c "import duckdb; c = duckdb.connect(); c.execute(\"SET extension_directory = '/app/duckdb_ext'\"); c.execute('INSTALL azure'); c.execute('LOAD azure')" \
    && test -n "$(find /app/duckdb_ext -name 'azure.duckdb_extension')"

# ---- runtime: shared base (ODBC 18, non-root user) -------------------------------------------
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    DUCKDB_EXTENSION_DIRECTORY=/app/duckdb_ext

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# ODBC Driver 18 for SQL Server (pyodbc). The Microsoft apt signing key is accepted only if the
# file holds exactly one primary key with the expected fingerprint. Both packages are version-pinned
# (bump together with the base digest; `apt-cache policy` shows what the repo currently offers).
ARG MS_KEY_FPR=BC528686B50D79E339D3721CEB3E94ADBE1229CF
ARG MSODBCSQL_VERSION=18.7.1.1-1
ARG UNIXODBC_VERSION=2.3.11-2+deb12u1
# (DL3008: only the throwaway bootstrap tools ca-certificates/curl/gnupg are unpinned; both runtime
# packages are pinned and the tools are purged in the same layer.)
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc -o /tmp/microsoft.asc \
    && colons="$(gpg --show-keys --with-colons /tmp/microsoft.asc)" \
    && [ "$(printf '%s\n' "$colons" | grep -c '^pub:')" -eq 1 ] \
    && fpr="$(printf '%s\n' "$colons" | awk -F: '$1=="pub"{p=1;next} p&&$1=="fpr"{print $10;exit}')" \
    && [ "$fpr" = "$MS_KEY_FPR" ] \
    && gpg --batch --dearmor -o /usr/share/keyrings/microsoft-prod.gpg /tmp/microsoft.asc \
    && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        "unixodbc=${UNIXODBC_VERSION}" "msodbcsql18=${MSODBCSQL_VERSION}" \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* /tmp/microsoft.asc /root/.gnupg

RUN groupadd --system --gid 10001 tidal \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tidal
WORKDIR /app
USER 10001:10001

# ---- dev: runtime for docker compose ----------------------------------------------------------
FROM runtime AS dev
COPY --from=builder --chown=10001:10001 /app /app
USER 10001:10001
EXPOSE 8000
CMD ["tidal-api"]

# ---- prod: hardened image (default target) ----------------------------------------------------
# Root-owned, read-only code (no --chown): the runtime user cannot modify the venv or sources.
FROM runtime AS prod
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/sql /app/sql
COPY --from=builder /app/duckdb_ext /app/duckdb_ext
USER 10001:10001
EXPOSE 8000
CMD ["tidal-api"]
