# Cost Model (living document)

All figures are **rough planning estimates in EUR/month, westeurope**, to be replaced with values
computed from `prices.yaml` (T-501) and measured costs (T-503/T-504). Any PR that adds or resizes an
Azure resource updates this table.

Assumption "active": ~40 h/month of real usage; rest of the month parked (`make park`).

| Component | SKU | Active-month estimate | Parked estimate | Lever |
|---|---|---|---|---|
| AKS control plane | Free tier | 0 | 0 | tier |
| AKS system pool | 1× B2s | 10–35 (35 if never stopped) | 0 (`az aks stop`) | stop cluster |
| AKS spot pool | D4as_v5 spot, 0–10 | 1–5 (only during sweeps) | 0 | spot + KEDA min 0 |
| AKS disks / LB / public IP | managed | 3–6 | 1–4 | stop cluster |
| Container Apps | Consumption | 0–3 (free grant covers most) | 0 | min replicas 0 |
| App Service | B1 Linux | ~12 | ~12 (or delete via flag) | smallest SKU; `enable_app_service` flag |
| Azure SQL | GP serverless 0.5–2 vCore, 32 GB | 5–20 | ~4 (storage only, paused) | auto-pause |
| Storage | LRS, tiered | 1–5 (M scale) | 1–5 | Parquet + tiers |
| Service Bus | Basic | < 1 | < 1 | SKU |
| ACR | Basic | ~5 | ~5 | purge |
| Key Vault | Standard | < 1 | < 1 | — |
| Log Analytics | PerGB, 0.2 GB/day cap | 0–5 | 0–1 | cap, sampling |
| VM legacy | B1s + disk | 2–4 | ~1 (disk) | schedule |
| **Total** | | **≈ 40–100** | **≈ 25** (≈ 13 with `enable_app_service=false`) | |

Observations to act on (before Phase 3):
- App Service B1 and ACR Basic dominate the parked cost → `enable_app_service` flag to delete when parked;
  consider keeping images in GHCR during development and pushing to ACR only for demos (document trade-off).
- Target ≤ €60 active is realistic only with disciplined parking; budget alerts enforce it.
