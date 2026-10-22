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
| AKS spot pool | D4as_v5 spot, 0–10 | 1–5 (only during sweeps) | 0 | spot + KEDA min 0 |
| AKS disks / LB / public IP | managed | 3–6 | 1–4 | stop cluster |
| Container Apps | Consumption | 0–3 (free grant covers most) | 0 | min replicas 0 |
| App Service | B1 Linux | ~12 | ~12 (or delete via flag) | smallest SKU; `enable_app_service` flag |
| Azure SQL | GP serverless 0.5–2 vCore, 32 GB | 0 on the free offer via azapi (ADR-0011); list 13.95 (0.5 vCore × 40 h) – 44.25 (2 vCore × 40 h) | 0 (free) / 3.85 list (storage only) | auto-pause; free offer is a dev-only lever (T-123 delta) |
| Storage | LRS Hot, flat namespace, tiered by `age_band` | ≈ 0.02 at rest (M scale, 3 GB) + transactions | ≈ 0.02 | Parquet + tiers (T-121 delta; transactions measured in T-503) |
| Service Bus | Basic | ≈ 0.001 (30 × 100-job sweeps) | 0 | SKU (T-124 delta); Standard (topic flag) not yet priced → T-502b |
| ACR | Basic | 4.40 (0.1466/day) + 0.088/GB over 10 GB | 4.40 | weekly purge (T-122 delta) |
| Key Vault | Standard | 0.03–0.26 (10k–100k ops) | < 0.03 | — (T-121 delta) |
| Log Analytics + App Insights | PerGB2018, 0.2 GB/day cap (≈ 6 GB/month), 30 d | ≤ cap × price (first 5 GB/month free) | ≈ 0 | cap, SDK sampling 20 % (T-120 delta) |
| VM legacy | B1s + disk | 2–4 | ~1 (disk) | schedule |
| Logic App (VM start) | Consumption | < 0.1 | < 0.1 | ≈ 2 actions/day |
| Budget + action group + TF state | — / email / LRS few KB | 0 / 0 / < 0.01 | same | free (T-125, T-008 deltas) |
| **Total** | | **≈ 40–100** | **≈ 25** (≈ 13 with `enable_app_service=false`) | |

Observations to act on (before Phase 3):
- App Service B1 and ACR Basic dominate the parked cost → `enable_app_service` flag to delete when parked;
  consider keeping images in GHCR during development and pushing to ACR only for demos (document trade-off).
- Target ≤ €60 active is realistic only with disciplined parking; budget alerts enforce it.
