# ADR-0007: Baseline dependency set
- Status: Proposed · Date: 2026-10-21 · Related: CLAUDE.md §3, PARALLELISM §1.7, T-001, plan review 2026-10-21 #2–#3
## Context
CLAUDE.md §3 forbids new dependencies without an ADR, but the specs already require libraries that are
not in the stack table (numpy for the generator, PyJWT for auth, schemathesis, vcrpy, matplotlib, locust,
a ULID library for job IDs, an OTel distro). Parallel lanes editing `pyproject.toml`/`uv.lock` would also
collide on every merge.
## Options
1. **Declare the full set up-front** in T-001; lanes never touch `pyproject.toml`.
2. Add dependencies per task with one ADR each — accurate, but 8+ ADRs and lock-file conflicts in every wave.
## Decision
Option 1. Runtime: polars, pyarrow, duckdb, numpy, fastapi, uvicorn[standard], pydantic, pydantic-settings,
httpx, PyJWT[crypto], python-ulid, pyyaml, typer (CLI), structlog, sqlalchemy, pyodbc, azure-identity,
azure-storage-blob, azure-servicebus, azure-keyvault-secrets, azure-monitor-opentelemetry.
Dev group: pytest, pytest-cov, pytest-benchmark, hypothesis, testcontainers, schemathesis 4.x, vcrpy, ruff,
mypy, pre-commit, pyarrow-stubs, types-pyyaml. Optional groups: `bench` (matplotlib), `load` (locust).
Non-Python tools via npx/CI only: oasdiff, redocly, gitleaks, tflint, checkov, trivy, syft.
## Consequences
One lock file from day one; image size grows (measured in T-305, SPEC-07 §4). Removing an unused
dependency needs no ADR; adding one does. Revisit at G3 when the image size is known.
