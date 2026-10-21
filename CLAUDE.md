# CLAUDE.md — Tidal

> Project memory for Claude Code. Read this file fully at the start of every session.
> Then read `docs/PLANNING.md` (the why), `docs/tasks/TASKS.md` (the what), and the spec + contract
> for the task you are on. Never start coding without a task ID.

## 1. What Tidal is

Tidal is a **cloud-native time-series platform for front-office trading** on Azure. It ingests
20+ years of energy & commodity market data (plus synthetic tick data at scale), stores it in a
cost-optimised **epoch time-series format** with **hot/cold tiering**, serves it through a secure
API, and runs **backtests at scale on AKS** with queue-driven autoscaling.

It is a portfolio project. Every design decision must be **measurable** and every cost claim must
be **backed by a number in `docs/finops/`**. "It's cheaper" without a benchmark is a bug.

## 2. Non-negotiable principles

1. **Contracts first.** Anything that crosses a module boundary (schemas, API, messages, storage
   paths, config keys) is defined in `docs/contracts/` and mirrored in `src/tidal/contracts/`.
   Changing a contract requires an ADR and a `contract-change` label on the PR.
2. **Deterministic numerics.** Prices, returns, P&L, backtest metrics and cost numbers come from
   deterministic code with seeded randomness. No LLM ever produces a number in this system.
3. **No secrets in code, config, or CI.** Managed Identity + Key Vault in Azure; GitHub OIDC
   federated credentials in CI; `.env` only for local emulators and is git-ignored.
4. **Everything is IaC.** If it exists in Azure, it exists in `infra/terraform/`. No portal clicks
   except reading Cost Management.
5. **Cost is a feature.** Every new Azure resource needs: SKU justification, scale-to-zero or
   auto-pause plan, and a cost line — in `docs/finops/deltas/T-xxx.md`, which the orchestrator folds
   into `docs/finops/cost-model.md` at the end of the wave.
6. **Small PRs.** One task ID per branch, < ~400 changed lines excluding generated files/tests.
7. **Tests are part of done.** Unit + contract tests for every task; integration tests where a
   boundary is touched.

## 3. Tech stack (do not add dependencies without an ADR)

| Layer | Choice |
|---|---|
| Language | Python 3.12, managed with **uv** |
| Data | **polars**, **pyarrow**, **duckdb** (with `azure` extension) |
| API | **FastAPI**, pydantic v2, pydantic-settings |
| Azure SDKs | azure-identity, azure-storage-blob, azure-servicebus, azure-keyvault-secrets |
| Relational | Azure SQL (serverless) via SQLAlchemy 2 + pyodbc (ODBC Driver 18) |
| Observability | structlog (JSON), OpenTelemetry → Azure Monitor / App Insights |
| Tests | pytest, hypothesis, pytest-benchmark, testcontainers (Azurite, SQL Server container) |
| Quality | ruff (lint+format), mypy `--strict`, pre-commit |
| Containers | Docker multi-stage, distroless/python or `python:3.12-slim` |
| Orchestration | AKS + **KEDA**, Helm 3; Azure Container Apps; App Service; 1 legacy VM |
| IaC | **Terraform** (azurerm ~> 4.x), remote state in Azure Storage |
| CI/CD | GitHub Actions with OIDC → Azure |

The full baseline set (incl. numpy, pyyaml, httpx, PyJWT[crypto], python-ulid, azure-monitor-opentelemetry,
schemathesis, vcrpy, matplotlib, locust) is fixed in ADR-0007 and installed by T-001. Anything else needs a new ADR.

## 4. Repository layout

```
src/tidal/
  common/        config (pydantic-settings), logging, time & epoch utils, errors
  contracts/     pydantic models + arrow schemas mirroring docs/contracts (single source in code)
  ingest/        sources/{eia,fred,synthetic}.py, normaliser, validators
  storage/       parquet_writer, layout (path builder), sql_repo, tiering
  query/         unified reader (SQL hot + Blob cold), duckdb engine
  api/           FastAPI app, routers, auth (Entra ID JWT), dependencies
  worker/        Service Bus consumer, job runner
  backtest/      deterministic strategy engine + metrics
  finops/        cost exporter, report generator
benchmarks/      storage-format & query benchmarks (pytest-benchmark + scripts)
infra/terraform/ modules/* and envs/dev
deploy/          docker/, helm/tidal/, k8s/ (KEDA ScaledJob etc.)
tests/           unit/ contract/ integration/ load/
docs/            PLANNING, business-knowledge, specs, contracts, tasks, adr, finops
```

## 5. Commands

```bash
uv sync                               # install
uv run pre-commit run --all-files     # lint/format/type
uv run pytest -m "not integration"    # fast tests
uv run pytest -m integration          # needs docker (Azurite, SQL Server container)
uv run pytest benchmarks/ --benchmark-only
docker compose up -d                  # local stack: azurite, sqlserver, servicebus-emulator, api
make tf-plan ENV=dev                  # terraform plan (never apply from a subagent)
make cost-report                      # regenerate docs/finops/results/*.md
```

## 6. Working agreement for Claude Code

- **Pick a task:** `/next-task` → read its spec section, contract(s), and dependencies' outputs.
- **Branch:** `feat/T-xxx-short-slug` (or `fix/`, `chore/`, `docs/`, `infra/`). One task per branch.
- **Ownership:** only touch files listed under the task's `Owns` column. Need another file? Stop,
  note it in the task, and ask the orchestrator (main session).
- **Done = all of:** acceptance criteria met · tests green · ruff/mypy clean · docs/spec updated if
  behaviour changed · cost-model updated if Azure resources changed · PR description filled.
- **Parallel work** follows `docs/PARALLELISM.md` (waves, worktrees, merge order). Subagents never
  merge, never `terraform apply`, never push to `main`.
- **Gates:** do not start a phase until the previous phase's gate checklist in `PLANNING.md` passes.
- When unsure about trading semantics, read `docs/business-knowledge.md` before guessing.

## 7. Coding conventions

- Timestamps: **UTC, int64 epoch nanoseconds** internally (`ts_ns`). Convert only at API edges.
  Never use naive `datetime`. Helpers live in `tidal.common.timeutil`.
- Prices: `float64` in storage; **negative prices are valid** (WTI 2020-04-20). Never clamp.
- Identifiers: instruments use `instrument_id` from the contract (e.g. `EIA.BRENT.SPOT`).
- Errors: raise typed errors from `tidal.common.errors`; API maps them to RFC 7807 problem+json.
- Logging: structured, no f-string interpolation of secrets/PII, always include `correlation_id`.
- Functions are pure where possible; I/O at the edges; dependency injection for Azure clients so
  everything is testable against Azurite/emulators.
- Public functions have docstrings and type hints. No `Any` without a comment explaining why.

## 8. Subagents (see `.claude/agents/`)

| Agent | Use for |
|---|---|
| `architect` | Plans, contract design, ADRs, reviewing cross-cutting changes (read-mostly) |
| `data-engineer` | ingest/, storage/, query/, benchmarks/ |
| `api-engineer` | api/, auth integration, OpenAPI conformance |
| `platform-engineer` | Docker, Helm, KEDA, AKS/Container Apps/App Service deploy manifests |
| `infra-engineer` | Terraform modules & envs |
| `security-reviewer` | Read-only security review of any PR touching auth, IaC, secrets, network |
| `finops-analyst` | Cost model, measurement scripts, cost reports |
| `test-engineer` | Test strategy, contract tests, integration & load tests |
| `code-reviewer` | Read-only PR review against CLAUDE.md + spec + contract |
| `docs-writer` | README, runbooks, ADR polishing, diagrams |

## 9. Definition of "portfolio-ready"

A reviewer can: read the README in 5 minutes, run `docker compose up` locally, see the
architecture diagram, open `docs/finops/cost-optimization.md` and find **before/after numbers for
every optimisation** listed in the job description, and browse PRs that show real review history.
