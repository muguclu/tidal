# Tidal — Project Plan

## 1. Goal

Build a portfolio-grade platform that proves, with code and measured numbers, the skills a
**front-office trading cloud/data engineer** role on Azure requires:

| Job requirement | Where Tidal proves it |
|---|---|
| Systems for large datasets, models over 20+ years of data | Synthetic 20+ yrs daily history (ADR-0013) + synthetic tick generator up to ~1.5B rows; backtests over full history (SPEC-01, SPEC-05) |
| AKS orchestration, Docker containerisation | Backtest workers on AKS with KEDA, multi-stage images (SPEC-07) |
| App Service, AKS, VMs, Container Apps for diverse workloads incl. legacy & serverless | API on App Service, query service on Container Apps (scale-to-zero), workers on AKS, "legacy" nightly exporter on a VM (SPEC-07) |
| ACR | Images built in CI, pushed to ACR, pulled via managed identity, vulnerability scanning (SPEC-07, SPEC-08) |
| AAD/Entra ID, RBAC, Key Vault | Entra ID JWT on API, app roles, Azure RBAC least privilege, Key Vault + managed identities, zero secrets (SPEC-06) |
| Azure SQL, Service Bus, Blob hot/cold | Hot window in Azure SQL serverless, jobs on Service Bus, cold history on Blob with lifecycle tiers (SPEC-02, SPEC-03) |
| **Cost optimisation (must have)** | Epoch Parquet format, SQL→object storage migration, lifecycle tiering, KEDA scale-to-zero + spot, SQL auto-pause, report with before/after (SPEC-09) |
| **Engineering practices (must have)** | Branch protection, CODEOWNERS, PR reviews, ADRs, unit/contract/integration tests, CI gates (SPEC-08, `engineering-practices.md`) |
| Communication | ADRs, specs, cost report written for a business audience, README, demo video |

## 2. Scope

**In scope**
- Daily history: Brent, WTI, Henry Hub, NBP-like proxy, power proxy, FX (EUR/USD) — 20+ years.
- Synthetic tick/1-second data for ~10 instruments × 20 years, configurable scale (S/M/L).
- Storage benchmark: row-store SQL vs CSV vs naive Parquet vs **epoch-optimised Parquet**.
- Hot/cold tiering with automated migration and lifecycle management.
- Query API (bars, ticks, spreads incl. M1–M2 from synthetic futures contracts 1–4, as-of) with auth and RBAC.
- Backtest jobs: deterministic strategies (moving-average crossover, mean reversion, calendar
  spread) over full history, fan-out on AKS.
- FinOps: cost model, measured results, budget alerts, a written report.

**Out of scope**
- Real-time streaming market data feeds, order routing, real trading.
- Licensed market data (ICE/CME). Only free/public sources + synthetic.
- Frontend beyond a minimal Swagger + an optional small dashboard page.
- Multi-region DR (documented as future work in an ADR).

## 3. Architecture (logical)

```
          ┌──────────────┐  daily job (Container Apps Job)
Synthetic ─▶ ingest       │──────────────┐
synthetic ▶ (normalise,  │              ▼
            validate)    │      ┌───────────────┐   migrate > 90d   ┌─────────────────────────┐
          └──────────────┘      │ Azure SQL     │ ───────────────▶ │ Blob: epoch Parquet     │
                                │ serverless    │                   │ hot → cool → cold →     │
                                │ (hot window)  │                   │ (no Archive for md/)    │
                                └──────┬────────┘                   └───────────┬─────────────┘
                                       │ unified reader (SQL ∪ Parquet via duckdb)
                                       ▼                                         │
 Trader/Quant ──Entra ID JWT──▶ API (App Service) ──▶ query svc (Container Apps, 0→N)
                                       │
                                       │ POST /backtests → Service Bus queue `backtest-jobs`
                                       ▼
                         AKS: KEDA ScaledJob (spot node pool, 0→N) ──▶ results to Blob + SQL
                                       │
                         Legacy VM: nightly CSV export for "downstream risk system"

Cross-cutting: Key Vault · Managed Identities · ACR · Log Analytics/App Insights · Budgets
```

## 4. Phases & gates

Each phase ends with a **gate**. The orchestrator verifies the gate checklist before the next
phase's wave starts. Waves and parallel lanes are detailed in `PARALLELISM.md`.

### Phase 0 — Foundation (Wave 0, mostly sequential)
Repo scaffold, tooling, CI skeleton, contracts v1 frozen, ADR-0001..0005, Terraform backend.
Items marked `NEEDS-CLOUD` are written and validated locally in their phase but only *executed* in
the cloud window (`LOCAL-DEV.md` §2); `/gate` reports them as NEEDS-CLOUD, which does not block the gate.

**Gate G0**
- [x] `uv sync && pre-commit && pytest` green in CI on an empty skeleton
- [x] All contracts in `docs/contracts/` reviewed and tagged `contracts-v1`
- [x] `src/tidal/contracts/` models generated & contract tests pass
- [x] Branch protection + CODEOWNERS + PR template active on `main`
- [x] Terraform bootstrap + OIDC module `terraform validate` clean (`NEEDS-CLOUD`: state account applied, OIDC login works)

G0 passed 2026-10-22 — evidence in `docs/status/2026-W43-G0.md`; tag `contracts-v1`.

### Phase 1 — Data core & infra base (Wave 1, 3 parallel lanes)
Lane A data: synthetic generator, daily-history generator, normaliser, epoch Parquet writer, layout.
Lane B infra: Terraform modules (rg, storage, kv, acr, sql, servicebus, log analytics, budgets).
Lane C api: FastAPI skeleton against contract with in-memory fake repository.
**Gate G1**
- [x] 20+ years of synthetic daily history (2005→, 12 series, `tidal synth daily-history`; ADR-0013) ingested locally into Parquet + SQL Server container
- [x] Synthetic S-scale (≈ 50M rows) generated reproducibly from a seed
- [ ] `terraform validate` + tflint + checkov clean for `envs/dev` (`NEEDS-CLOUD`: plan clean, applied once by a human, outputs recorded)
- [x] API returns contract-valid responses from fakes; `oasdiff breaking` reports nothing in either direction

### Phase 2 — Storage strategy & query (Wave 2, 3 lanes)
Lane A: storage benchmark (4 formats) + results. Lane B: tiering/migration job + lifecycle
policy. Lane C: unified reader + duckdb engine wired into API.
**Gate G2**
- [x] Benchmark report committed with size, write time, scan time, point-query latency, €/month
- [x] Migration moves rows older than the hot window from SQL → Parquet idempotently
- [x] API queries spanning hot+cold return identical results to a pure-SQL baseline (property test)

### Phase 3 — Compute & platform (Wave 3, 3 lanes)
Lane A: backtest engine + worker. Lane B: Docker images, Helm chart, KEDA ScaledJob, AKS spot
pool. Lane C: App Service + Container Apps + VM legacy exporter deployments.
**Gate G3**
- [ ] 100 backtest jobs enqueue → AKS scales 0→N → completes → scales back to 0 (recorded)
- [ ] Every workload deployed via CI from ACR with managed identity pull
- [ ] Container Apps query service scales to zero when idle (metric screenshot)

### Phase 4 — Security hardening (Wave 4, 2 lanes + review)
Entra ID app registrations & roles, RBAC matrix, Key Vault references, private endpoints for
SQL/Blob/KV, Defender for Containers scan in CI, threat model.
**Gate G4**
- [ ] `security-reviewer` sign-off on SPEC-06 checklist
- [ ] No secret in repo (gitleaks), no connection string with key in any app setting
- [ ] RBAC matrix tested: trader role cannot submit backtests; quant can; reader cannot write

### Phase 5 — FinOps & observability (Wave 5, 2 lanes)
Cost exporter, measured before/after for each optimisation, dashboards, alerts, budget.
**Gate G5**
- [ ] `docs/finops/cost-optimization.md` has a measured number for every row in SPEC-09 §3
- [ ] Budget alert + anomaly alert configured in Terraform

### Phase 6 — Polish (Wave 6)
README, architecture diagram, runbooks, demo script, 3-minute video, LinkedIn post draft.
**Gate G6 = portfolio-ready** (see CLAUDE.md §9).

## 5. Timeline (part-time, ~10 h/week)

| Week | Phase | Milestone |
|---|---|---|
| 1 | P0 | Contracts frozen, CI green |
| 2–3 | P1 | Data in Parquet, infra planned, API on fakes |
| 4–5 | P2 | Benchmark published, tiering live |
| 6–7 | P3 | AKS/KEDA demo recorded |
| 8 | P4 | Security sign-off |
| 9 | P5 | Cost report |
| 10 | P6 | Public launch |

The plan has ~75 tasks; at ~10 h/week the 10-week timeline is optimistic (plan-review 2026-10-21 #31).
The **"minimum hireable"** cut — P0–P2 + the AKS part of P3 + a cost report for what exists — is the
primary target; P4–P6 are stretch. Re-plan at every gate using actual velocity.

## 6. Budget guardrails (personal subscription)

Local-first: Phases 0–3 need no Azure account. Azure is used in a ~30-day cloud window (free
account credit) for evidence — see `docs/LOCAL-DEV.md`.


- Target **≤ €60/month** while active, **≤ €15/month** when parked (App Service removed via flag).
- `make park` = scale AKS user pool to 0, stop system pool (`az aks stop`), deallocate VM,
  SQL auto-pause, Container Apps min replicas 0. `make unpark` reverses.
- Terraform budget resource with alerts at 50/80/100 % + cost anomaly alert.
- See `docs/finops/cost-model.md` for per-resource estimates.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| AKS cost overruns | € | Free tier control plane, B-series system node, spot user pool min 0, `make park` |
| Free-trial vCPU quota (≈ 4 vCPU/region, no spot) blocks the 0→20-pod demo | G3 not provable | Upgrade to PAYG and request quota (DASv5 + spot ≥ 24 vCPU) **before** cloud-window day 11; budget alerts on (LOCAL-DEV §2) |
| Azure SQL free offer (32 GB, 100k vCore-s/month) too small for M-scale SQL benchmark | Benchmark incomplete | Azure SQL formats run at S scale; M-scale SQL numbers are MODELLED from local runs (SPEC-02 §5) |
| Spot eviction mid-backtest | Jobs fail | Jobs are idempotent & checkpointed; KEDA retries; DLQ after 3 attempts |
| ~~Free API rate limits (EIA/FRED)~~ | Closed: obsolete via ADR-0013 (no vendor feeds) | — |
| Synthetic data looks fake | Credibility | Calibrate GBM + jumps + regime switches to real daily vol; document it |
| Scope creep | Never ships | Gates + "minimum hireable" cut + out-of-scope list |
| Parallel agents conflict | Rework | File ownership map, contracts frozen before waves, merge order |

## 8. Success metrics (fill in at the end)

| Metric | Target |
|---|---|
| Storage size epoch Parquet vs SQL rowstore | ≥ 10× smaller |
| Full-history scan, 1 instrument, 20 yrs ticks | < 5 s on Container Apps 1 vCPU |
| Monthly storage € at L-scale, tiered vs all-hot SQL | ≥ 80 % lower |
| Backtest compute €/100 jobs, spot+KEDA vs always-on | ≥ 60 % lower |
| Test coverage (src/) | ≥ 85 % |
| PRs merged with review | 100 % |
