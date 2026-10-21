# Tidal — Trading Time-Series Platform on Azure

> Cost-optimised platform for 20+ years of energy market data: epoch Parquet storage, hot/cold tiering,
> secure API, and backtests at scale on AKS with KEDA.

**Status:** planning complete · implementation starting (see `docs/tasks/TASKS.md`)

## Results (filled in Phase 5–6 from `docs/finops/cost-optimization.md`)

| Optimisation | Baseline | Tidal | Saving |
|---|---|---|---|
| Time-series storage size | SQL rowstore | epoch Parquet | _tbd_ |
| SQL → object storage | all history in SQL | 90 d hot + Blob | _tbd_ |
| Backtest compute | fixed on-demand nodes | KEDA + spot, 0→N | _tbd_ |
| Idle API/query cost | always-on | Container Apps scale-to-zero | _tbd_ |

## Architecture
See `docs/PLANNING.md` §3 (diagram added in T-601).

## Documentation map

| File | What it is |
|---|---|
| `CLAUDE.md` | Rules for Claude Code sessions |
| `docs/PLANNING.md` | Goals, scope, architecture, phases & gates, budget, risks |
| `docs/business-knowledge.md` | Trading domain knowledge and data-quality rules |
| `docs/PARALLELISM.md` | Waves, lanes, ownership, merge order, orchestrator loop |
| `docs/tasks/TASKS.md` | Backlog with dependencies and done criteria |
| `docs/specs/SPEC-01…10` | Functional & technical specs |
| `docs/contracts/` | Data, storage, API (OpenAPI), messaging, config contracts |
| `docs/adr/` | Architecture decisions |
| `docs/finops/` | Cost model, prices, measurements, final report |
| `docs/engineering-practices.md` | Branching, reviews, testing, quality |
| `.claude/agents/` | Subagents (architect, data, api, platform, infra, security, finops, test, reviewer, docs) |
| `.claude/commands/` | `/next-task`, `/wave`, `/gate`, `/review`, `/adr` |
| `docs/LOCAL-DEV.md` | €0 local stack + 30-day Azure cloud window plan |
| `docs/KICKOFF.md` | How to start building with Claude Code |
