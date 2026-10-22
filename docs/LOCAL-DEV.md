# LOCAL-DEV — Zero-cost development and the 30-day cloud window

Tidal is built **local-first**. Phases 0–3 run entirely on a laptop for €0. Azure is used only in a
planned, time-boxed **cloud window** to collect real evidence (deployments, scaling, Cost Management).

## 1. Local equivalents

| Azure service | Local stand-in | Notes |
|---|---|---|
| Blob Storage | **Azurite** (`mcr.microsoft.com/azure-storage/azurite`) | Same SDK; lifecycle policies and access tiers are *not* emulated → test tag logic only |
| Azure SQL | **SQL Server 2022 container** (`mcr.microsoft.com/mssql/server:2022-latest`) | Azure SQL Edge is retired. On Apple Silicon runs via Rosetta (enable in Docker Desktop). Serverless auto-pause not emulated |
| Service Bus | **Service Bus emulator** (`mcr.microsoft.com/azure-messaging/servicebus-emulator`) | Shares the compose SQL Server container (T-112); queues in `deploy/local/sb-config.json`. Emulator caps message TTL at 1 h (Azure: 24 h per contract) and has no duplicate detection |
| AKS | **kind** or **k3d** + KEDA Helm chart | Scaling demo 0→N→0 works locally; if the KEDA azure-servicebus scaler cannot reach the emulator, use the `redis` or `metrics-api` scaler for the local demo only (document it) |
| Container Apps / App Service | docker compose services | Scale-to-zero only demonstrable in Azure |
| Entra ID | Local test JWKS + token minting script (`scripts/dev-token.py`) | Real Entra only in the cloud window |
| Key Vault | `.env.local` (git-ignored) | Code path uses `DefaultAzureCredential` + KV only when `TIDAL_ENV != local` |
| Terraform | `terraform validate`, `tflint`, `checkov`, `terraform plan` needs a subscription | Write and lint all modules locally |
| Prices for FinOps | **Azure Retail Prices API** (public, no account) | All MODELLED cost numbers can be produced without Azure |

## 2. The cloud window (≈ 30 days, Phases 3-end → 5)

Azure free account (new customers): **USD 200 credit valid 30 days**, plus 12-month free amounts and
always-free services. After 30 days, upgrade to pay-as-you-go or the account is disabled.

Important constraints:
- Free credit **cannot be used for Spot VMs**, and free-trial vCPU quota is tiny (≈ 4 vCPU per region,
  not raisable). The 0→20-pod KEDA demo is therefore impossible on the trial. **Decision:** upgrade to
  PAYG (credit is kept) and request quota (Standard DASv5 family ≥ 24 vCPU, spot ≥ 24 vCPU) on day 1,
  so it is approved before day 11. Without approval: run the demo at the quota maximum and model the rest.
- **Azure SQL Database free offer**: up to 10 free GP serverless DBs per subscription, 100,000 vCore-seconds
  + 32 GB each per month, lifetime of the subscription; set "auto-pause until next month" on limit.
  Use it for `tidal` DB → SQL cost ≈ €0 (note in cost report as a dev-only lever).
- Container Apps consumption has a monthly free grant → query service ≈ €0 at demo traffic.

Window plan:
| Day | Action |
|---|---|
| 1 | Create free account, budget alert, bootstrap TF state, GitHub OIDC |
| 2–5 | `terraform apply` core (storage, KV, SQL free, SB Basic, ACR, LA, Container Apps), deploy API/query |
| 6–10 | M-scale data upload, storage benchmark absolute numbers, tiering run, lifecycle evidence |
| 11–15 | AKS (Free tier, B2s system + D-series user pool min 0), 100-job KEDA demo, recordings |
| 16–20 | Entra app roles, RBAC tests, private networking flag on/off once, security screenshots |
| 21–27 | Cost exports, Cost Management screenshots, FinOps report numbers |
| 28–30 | Demo video, then `terraform destroy` (keep SQL free DB + storage if upgrading to PAYG) |

Rule: **`make park` every evening**, budget alert at USD 50 / 100 / 150 of the credit.
