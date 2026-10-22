# Runbook: integration tests (T-128)

CI runs `uv run pytest -m integration` in `.github/workflows/integration.yml` on PRs that touch
`src/`, `tests/integration/`, `benchmarks/`, `sql/`, `pyproject.toml`, `uv.lock` or the workflow, on
manual dispatch, and nightly (`TIDAL_TEST_CRASH_EXAMPLES=40`, `TIDAL_TEST_CRASH_FULL=1`). It is advisory,
not a required check. The suite needs Docker and ODBC Driver 18 for SQL Server (`unixodbc` +
`msodbcsql18`; `pyodbc.drivers()` must list it, otherwise the SQL suites skip).

## Run locally
Use a throwaway SQL Server with a generated password (never reuse your compose password in shell history):

```bash
export TIDAL_SQL__PASSWORD="Aa1-$(openssl rand -hex 16)"
export TIDAL_TEST_SQL_PASSWORD="$TIDAL_SQL__PASSWORD" TIDAL_TEST_SQL_URL="127.0.0.1,1433"
docker run -d --name sql-it -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$TIDAL_SQL__PASSWORD" \
  -p 127.0.0.1:1433:1433 mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04
docker run -d --name azurite-it -p 127.0.0.1:10000:10000 mcr.microsoft.com/azure-storage/azurite:3.37.0 \
  azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
# Azurite dev account string from .env.example (public key) for the file-format benchmark suite:
export TIDAL_STORAGE__CONNECTION_STRING="$(grep '^TIDAL_STORAGE__CONNECTION_STRING=' .env.example | cut -d= -f2- | tr -d '"')"
uv run pytest -m integration --no-cov
docker rm -f sql-it azurite-it
```

If `docker compose up` is already running, skip the two `docker run` lines and use its `TIDAL_SQL__PASSWORD`
from `.env`. Suites that start their own containers (testcontainers) work either way.

## Knobs
| Variable | Used by | Effect |
|---|---|---|
| `TIDAL_TEST_SQL_URL` (`host,port`) + `TIDAL_TEST_SQL_PASSWORD` or `TIDAL_SQL__PASSWORD` | storage, query, bench SQL suites | reuse a running SQL Server (throwaway database per module); unset = testcontainer (or skip for bench/query-service) |
| `TIDAL_STORAGE__CONNECTION_STRING` | `tests/integration/bench` file formats | Azurite connection string; unset = skip |
| Azurite on `127.0.0.1:10000` | query-service suite | skips if unreachable |
| `TIDAL_TEST_REQUIRE_SQL=1` | `tests/integration/conftest.py` | runtime skips become failures (set in CI) |
| `TIDAL_TEST_AZURITE_CS` | crash suite (T-206) | Azurite service connection string instead of a testcontainer |
| `TIDAL_TEST_CRASH_EXAMPLES`, `TIDAL_TEST_CRASH_FULL` | crash-injection suite (T-206) | example count / full matrix |
