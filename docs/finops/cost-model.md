# Cost Model (living document)

All figures are **rough planning estimates in EUR/month, westeurope**, to be replaced with values
computed from `prices.yaml` (T-501) and measured costs (T-503/T-504). A PR that adds or resizes an
Azure resource writes `docs/finops/deltas/T-xxx.md`; the orchestrator folds deltas into this table per wave.

Assumption "active": ~40 h/month of real usage; rest of the month parked (`make park`).
Rows marked with a delta are MODELLED from `docs/finops/prices.yaml` (captured 2026-10-21) in `docs/finops/deltas/T-xxx.md`.

| Component | SKU | Active-month estimate | Parked estimate | Lever |
|---|---|---|---|---|
| AKS control plane | Free tier | 0 | 0 | tier |
| AKS system pool | 1× B2s | 10–35 (35 if never stopped) | 0 (`az aks stop`) | stop cluster |
| AKS spot pool | D4as_v5 spot, 0–10, 32 GB P4 OS disk | 1.63–6.12 (40–150 node-hours, incl. prorated OS disk) | 0 | spot + KEDA min 0 (T-306 delta) |
| AKS disks / LB / public IP | P4 system OS disk, Standard LB, static public IP | 24.38 (5.11 + 16.06 + 3.21) | 24.38 if `az aks stop` keeps LB + IP; 5.11 if released (unverified, T-313) | delete cluster when parked long (T-306 delta) |
| Container Apps | Consumption | 0 (assumed usage 50–81 % of the free grant; compute meters unpriced, T-309 delta) | 0 | min replicas 0; cron jobs suspended when parked |
| App Service | B1 Linux | 11.53 (730 h × 0.0158) | 11.53 (stopped B1 still bills) or 0 with `enable_app_service=false` | smallest SKU; `enable_app_service` flag (T-309 delta) |
| Azure SQL | GP serverless 0.5–2 vCore, 32 GB | 0 on the free offer via azapi (ADR-0011); list 13.95 (0.5 vCore × 40 h) – 44.25 (2 vCore × 40 h) | 0 (free) / 3.85 list (storage only) | auto-pause; free offer is a dev-only lever (T-123 delta) |
| Storage | LRS Hot, flat namespace; tick parts tiered Cool/Cold by `age_band` (2 index tags, ADR-0015), bars stay Hot | ≈ 0.04 at rest (M scale: 0.0516 all-Hot − 0.0100 net ticks-only tiering after index-tag cost) + transactions | ≈ 0.04 | Parquet (F5 is 3.6x smaller than SQL rowstore F0, T-204) + tiers (T-121, T-207, T-211; transactions measured in T-503) |
| Service Bus | Basic | ≈ 0.001 (30 × 100-job sweeps) | 0 | SKU (T-124 delta); Standard (topic flag) not yet priced → T-502b |
| ACR | Basic | 4.40 (0.1466/day) + 0.088/GB over 10 GB | 4.40 | weekly purge (T-122 delta) |
| Key Vault | Standard | 0.03–0.26 (10k–100k ops) | < 0.03 | — (T-121 delta) |
| Log Analytics + App Insights | PerGB2018, 0.2 GB/day cap (≈ 6 GB/month), 30 d | ≤ cap × price (first 5 GB/month free) | ≈ 0 | cap, SDK sampling 20 % (T-120 delta); alerts and workbook add ≈ 0.26, plus up to 1.89 blob-audit ingestion inside the cap (T-506 delta) |
| VM legacy | B1s + E4 StandardSSD | 2.16 (5 h compute + 2.11 disk) | 2.11 (disk) or 0 with `enable_legacy_vm=false` | daily start, self-deallocate (T-310 delta) |
| Logic App (VM start) | Consumption | 0 (≈ 30 built-in actions/month, free tier) | 0 | `start_enabled` (T-310 delta) |
| Budget + action group + TF state | — / email / LRS few KB | 0 / 0 / < 0.01 | same | free (T-125, T-008 deltas) |
<!-- total-row:start -->
| **Total** | | **≈ 68–127** (SQL at list; ≈ 54–83 on the free offer) | **≈ 47** (≈ 43 on the free offer; ≈ 35 with `enable_app_service=false`) | re-summed by `cost_optimization_report.py` (T-507) |
<!-- total-row:end -->

### Composed so far: `envs/dev` (T-126)

Subtotal of the components that `infra/terraform/envs/dev` composes today: storage, Key Vault, ACR, Azure SQL,
Service Bus, Log Analytics + App Insights, budget/action group (plus resource group and identities, 0). Every figure is a
sum of the rows above; nothing new is estimated.

| Subtotal (EUR/month) | Active month | Parked month |
|---|---|---|
| Composed, SQL on the free offer (`use_free_offer = true`, default) | ≈ 7.10 – 7.33 (0 + 0.04 + 0.001 + 4.40 + 0.03–0.26 + 2.63 Log Analytics ceiling) | ≈ 4.47 (0 + 0.04 + 0 + 4.40 + < 0.03) |
| Composed, SQL at list price (free offer off) | ≈ 21.1 – 51.6 (the line above + 13.95–44.25) | ≈ 8.3 (the line above + 3.85) |
| Not yet composed (T-306 AKS, T-309 App Service + Container Apps, T-310 VM + Logic App, T-404 network) | rows AKS ×4, Container Apps, App Service, VM, Logic App above | same rows |

The **Total** row above stays the target for the full environment; the subtotal above is what a plan of `envs/dev`
costs before the compute modules land. Log Analytics is a ceiling: the 0.2 GB/day cap gives about 6 GB/month, the first
5 GB/month are free, so (6 - 5) GB x 2.6311 EUR (`loganalytics_ingestion_gb` in `prices.yaml`) = 2.63. The 5 GB free tier
is shared across the billing account, so it is not guaranteed to be available to this environment. Parked months use the
row's ≈ 0. Service Bus Standard (`enable_ingest_topic = true`) is not in the subtotal (not yet priced, T-502b).

Observations to act on (before Phase 3):
- App Service B1 and ACR Basic dominate the parked cost → `enable_app_service` flag to delete when parked;
  consider keeping images in GHCR during development and pushing to ACR only for demos (document trade-off).
- Target ≤ €60 active is realistic only with disciplined parking; budget alerts enforce it.

Wave 5 fold (T-506, T-507): alert rules add 0.26 EUR/month (blob audit ingestion stays inside the 0.2 GB/day cap, so the Log
Analytics ceiling does not move). The Total row was re-summed by `docs/finops/cost_optimization_report.py` from `prices.yaml` and
the model scripts; the old 61–114 / 47 could not be reproduced (see the re-sum table in `cost-optimization.md`).

Wave 4 fold (T-401–T-407): no change to the totals with default flags (Entra apps free, RBAC and auditing about
0). Optional levers, from the T-404/T-407 model scripts: `enable_private_networking` +24.95 EUR/month active
(NAT variant 53.86), `enable_defender_for_sql` +13.20 EUR/month per server.

Wave 3 fold (T-309, T-310): App Service, Container Apps, legacy VM and Logic App rows now come from the
T-309/T-310 model scripts; totals re-summed (61–114 active, ≈ 47 parked). AKS LB + IP (T-306) still dominates the
parked figure.

Wave 3 fold (T-306): the AKS load balancer and public IP alone cost 19.27 EUR/month and do not stop with the
cluster unless `az aks stop` releases them (unverified). The old 3–6 estimate was too low; the totals above are
raised accordingly (61–120 active, ≈ 45 parked). Parking the AKS cluster for weeks therefore means deleting it
(T-313 decides), otherwise the ≤ €60 active target is not met. Figures from `docs/finops/deltas/T-306_model.py`.

Wave 2 fold (T-204, T-207, T-211):
- **Storage figure raised from ≈ 0.02 to ≈ 0.04.** The T-207 model (`docs/finops/deltas/T-207_model.py`, per-scale
  `age_band` split) puts M all-Hot at 0.0516 EUR/month; the earlier ≈ 0.02 used T-121's 20-year split at every scale.
- **Blob index tags cost money.** At 0.0343 EUR per 10K tags per month, five tags on every part made tiering a net loss at
  S, M and L. ADR-0015 tags tick parts only, with two tags: net +0.0010 / +0.0100 / +0.1108 EUR/month at S / M / L.
  Bars stay Hot. A dataset joins tiering when its measured average part size clears the 2-tag break-even
  (0.85–1.50 MB); re-check with T-503.
- **Format choice.** Locally measured (T-204, 3-month slice of S): F5 Parquet is 3.6x smaller than the SQL rowstore
  baseline F0 and 25.4x cheaper at rest at list price. Azure absolute numbers are NEEDS-CLOUD.
