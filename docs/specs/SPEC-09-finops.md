# SPEC-09 — FinOps: Cost Optimisation, Measurement & Report (MUST HAVE)

Owner agent: `finops-analyst` · Inputs from every lane · Tasks: T-501 … T-504, T-507 (T-505/T-506 in SPEC-10)

## 1. Purpose
Turn "cost optimisation" from a claim into evidence. Every optimisation has a **baseline**, an
**optimised** configuration, a **measurement method**, and a **number**.

## 2. Measurement sources
1. **Azure Cost Management exports** (daily, amortised) to `bench/cost-exports/` via Terraform
   `azurerm_subscription_cost_management_export`; analysed by `finops/cost_report.py`.
2. **Resource tags** (`component`, `lane`) for attribution.
3. **Metrics** (Azure Monitor): SQL vCore seconds, storage used capacity per tier, AKS node hours by pool,
   Container Apps replica seconds, Service Bus operations.
4. **Unit prices** from `docs/finops/prices.yaml` (Retail Prices API snapshot, westeurope, date) for
   modelled scenarios at scales we don't actually run (e.g. L-scale for 1 year).

Rule: report both **measured** (what the bill shows for the test period) and **modelled** (unit price ×
measured quantity, extrapolated to production-like volume), clearly labelled.

## 3. Optimisation register (each row = one section in the final report)

| # | Optimisation | Baseline | Optimised | Metric | Evidence |
|---|---|---|---|---|---|
| C1 | Epoch time-series format | F0 SQL rowstore | F5 epoch Parquet | GB, €/month at rest | SPEC-02 benchmark |
| C2 | SQL → object storage migration | all history in SQL GP 2 vCore | 90 d hot in SQL serverless + rest on Blob | SQL GB, SQL €/month, query € | SPEC-03 before/after `sp_spaceused` |
| C3 | Blob lifecycle tiering by data age | all Hot | Hot/Cool/Cold by `age_band` | €/month at rest incl. read & early-delete costs | tier capacity metric + model |
| C4 | Dynamic autoscaling (AKS) | fixed 5× D4as_v5 on-demand 24/7 | KEDA ScaledJob 0→N on spot | €/100 backtests, idle € | node-hours by pool |
| C5 | Serverless containers | query service on always-on B2 | Container Apps min 0 | €/month, p95 cold-start latency (trade-off!) | replica-seconds |
| C6 | SQL serverless auto-pause | provisioned GP 2 vCore | serverless 0.5–2, pause 60 min | €/month, resume latency | vCore-seconds |
| C7 | Scheduled VM | B1s 24/7 | auto-shutdown + scheduled start | €/month | VM running hours |
| C8 | Log ingestion control | default diag settings | daily cap, sampling 20 %, basic logs table | GB ingested/day | LA usage |
| C9 | ACR hygiene | no purge | weekly purge untagged + small images | GB stored | ACR usage |
| C10 | Parking | env always on | `make park` outside work hours | €/month | cost by day |

## 4. Trade-offs (must be written, not hidden)
Cold-start latency of scale-to-zero, SQL resume delay (~1 min), spot evictions, Cool/Cold read and
early-deletion charges, Archive rehydration time. A section "When I would NOT do this in production"
for each — this is what interviewers probe.

## 5. Deliverables
- `docs/finops/cost-model.md` — living per-resource estimate (updated by any PR adding resources).
- `docs/finops/prices.yaml` — pinned price snapshot + script `finops/fetch_prices.py`.
- `docs/finops/results/*.md` — raw measurement notes per optimisation.
- `docs/finops/cost-optimization.md` — **final report**: exec summary (≤ 5 bullets with € and %),
  table of C1–C10, charts, trade-offs, method, what I'd do at Shell/enterprise scale (reservations,
  savings plans, Azure Hybrid Benefit, dev/test pricing, FinOps governance).

## 6. Acceptance criteria
- [ ] Every C-row has baseline, optimised, measured or modelled number, and evidence link.
- [ ] Numbers reproducible from committed scripts + exports.
- [ ] Exec summary readable by a non-engineer trading manager.
