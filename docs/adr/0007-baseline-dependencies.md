# ADR-0007: Baseline dependency set, declared up-front

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [CLAUDE.md §3](../../CLAUDE.md), [PARALLELISM §1.7](../PARALLELISM.md),
  [plan review #2, #3](../reviews/2026-10-21-plan-review.md), [SPEC-07 §4](../specs/SPEC-07-infra-platform.md), T-001, T-305

## Context
CLAUDE.md §3 forbids new dependencies without an ADR, yet the specs already need libraries outside the
stack table: numpy (synthetic generator), PyJWT (auth), schemathesis and vcrpy (tests), matplotlib (charts),
locust (load), a ULID library (job IDs) and an OpenTelemetry distro. If each parallel lane added its own,
every merge would conflict on `pyproject.toml` and `uv.lock`. The job values **engineering practices**:
a reproducible, reviewed dependency set.

## Options
1. **Declare the full set up-front in T-001;** lanes never touch `pyproject.toml`.
   - Pros: one lock file from day one; no lock-file conflicts; one review of the whole set.
   - Cons: some packages are installed before they are used; larger image earlier.
   - € impact: no direct Azure cost; image size (ACR Basic ~€5/month, cost-model) measured in T-305.
2. **Add dependencies per task, one ADR each.**
   - Pros: each dependency arrives with its first user.
   - Cons: 8+ ADRs and a lock-file conflict in every wave.
   - € impact: same as option 1 once all are added.

## Decision
Option 1, because one up-front, reviewed set removes lock-file conflicts between parallel lanes.

- Runtime: polars, pyarrow, duckdb, numpy, fastapi, uvicorn[standard], pydantic, pydantic-settings, httpx,
  PyJWT[crypto], python-ulid, pyyaml, typer, structlog, sqlalchemy, pyodbc, azure-identity,
  azure-storage-blob, azure-servicebus, azure-keyvault-secrets, azure-monitor-opentelemetry.
- Dev group: pytest, pytest-cov, pytest-benchmark, hypothesis, testcontainers, **schemathesis 4.x**
  (pinned `>=4,<5`; loaders and pytest plugin differ from 3.x), vcrpy, ruff, mypy, pre-commit,
  pyarrow-stubs, types-pyyaml. Optional groups: `bench` (matplotlib), `load` (locust).
- Non-Python tools via npx/CI only: oasdiff, redocly, gitleaks, tflint, checkov, trivy, syft.

## Consequences
- Shared files (`pyproject.toml`, `uv.lock`, `Makefile`, `tests/conftest.py`, `docs/finops/cost-model.md`)
  are **orchestrator-only** after T-001; a lane that needs a change reports it and rebases.
- Adding a dependency needs an ADR; removing an unused one does not.
- Monitor: image size and top-5 layers (T-305, ≤ 500 MB), trivy findings, unused packages.
- Revisit at G3, when the image size is known, and on any schemathesis 5.x release.
