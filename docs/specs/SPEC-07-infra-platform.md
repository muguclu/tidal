# SPEC-07 — Infrastructure & Platform (Terraform, ACR, AKS, Container Apps, App Service, VM)

Owner agents: `infra-engineer` (Terraform), `platform-engineer` (Docker/Helm/KEDA)
Tasks: T-120 … T-126, T-305 … T-310

## 1. Terraform layout
```
infra/terraform/
  bootstrap/            state storage account (applied once, local state, documented)
  modules/
    resource_group/  log_analytics/  storage/  key_vault/  acr/  sql/  servicebus/
    aks/  container_apps/  app_service/  vm_legacy/  identity/  network/  budget/  entra_app/
  envs/dev/             main.tf composes modules; dev.tfvars; backend.tf (azurerm)
```
Rules: every module has `variables.tf` with descriptions + validation, `outputs.tf`, a `README.md`
(terraform-docs), and `examples/`. `tflint` + `checkov` in CI. Tags on everything:
`project=tidal, env, owner, cost_center=portfolio, park=true|false`.

## 2. Resources & SKUs (dev)

| Resource | SKU / setting | Cost lever |
|---|---|---|
| Log Analytics | PerGB2018, **daily cap 0.2 GB**, retention 30 d | cap |
| Storage | StorageV2 LRS Hot, flat namespace | lifecycle + tags |
| Key Vault | Standard, RBAC | — |
| ACR | **Basic** | retention: untagged manifests purged weekly by ACR Task (`acr purge`) |
| Azure SQL | **General Purpose Serverless** Gen5, min 0.5 / max 2 vCore, **auto-pause 60 min**, 32 GB | auto-pause |
| Service Bus | **Basic** namespace (Standard only if topic flag on) | SKU |
| AKS | **Free tier** control plane; system pool 1× `Standard_B2s`; user pool `spot` `Standard_D4as_v5`, min 0 / max 10, `spot_max_price = -1`, eviction `Delete`; KEDA add-on; workload identity + OIDC issuer on; Azure CNI Overlay | spot + scale-to-zero + `az aks stop` |
| Container Apps | Consumption environment; `tidal-query` app min 0 / max 5, HTTP scale rule 20 concurrent; Jobs: optional `synth-daily-history` (manual/cron, `tidal synth daily-history`), `tiering` (event: SB queue via KEDA scaler) | scale to zero |
| App Service | Linux **B1**, container from ACR, VNet integration (flag) | smallest always-on |
| VM (legacy) | `Standard_B1s` Ubuntu LTS, **auto-shutdown 20:00 CET**, started 21:55 by a Logic App (Consumption) schedule using its MI; the exporter deallocates the VM itself afterwards (custom role, SPEC-06 §2) | schedule; Logic App ≈ €0 (≈ 2 actions/day) |
| Budget | €60/month, alerts 50/80/100 %, action group email | guardrail |

## 3. Why each compute target (portfolio narrative — mirror in ADR-0002)
- **App Service** → classic always-on API with managed TLS, slots, easy Entra auth: the "traditional" PaaS.
- **Container Apps** → bursty, stateless, scale-to-zero query service + cron/event Jobs: serverless containers.
- **AKS** → large fan-out batch compute that needs node-level control (spot pools, KEDA ScaledJob, taints).
- **VM** → a "legacy-style" cron exporter (plain script + systemd timer, no container) that writes CSV for a downstream risk system; shows
  lift-and-shift with modern identity (managed identity, no keys) and cost control (schedule).

## 4. Containers
- `deploy/docker/Dockerfile` multi-stage: `uv` builder → `python:3.12-slim` (or distroless) runtime,
  non-root UID 10001, `PYTHONDONTWRITEBYTECODE`, healthcheck. One image, entrypoints:
  `tidal-api`, `tidal-query`, `tidal-worker`, `tidal-tiering` (plus the `tidal` CLI for `synth`).
- Image size: measure first (python-slim + pyarrow + polars + duckdb + msodbcsql18 is ~450 MB);
  target ≤ 500 MB, record the number and the top-5 layers in the PR. Build cache via GitHub Actions cache; tags: `sha-<short>`, `vX.Y.Z`;
  `latest` never used in deployments.
- `docker-compose.yml` for local: azurite, `mcr.microsoft.com/mssql/server:2022-latest` (Azure SQL Edge was retired Sep 2025), Service Bus emulator,
  api, query, worker.

## 5. Helm chart `deploy/helm/tidal`
- Templates: `serviceaccount` (workload identity annotations), `scaledjob-backtest`,
  `triggerauthentication` (`podIdentity: azure-workload`), `secretproviderclass`, `networkpolicy`
  (deny-all + allow egress 443), `poddisruptionbudget`, `resourcequota` for namespace `tidal`.
- `values-dev.yaml`; `helm lint`, `kubeconform`, `kube-linter` in CI.

## 6. Acceptance criteria
- [ ] `terraform plan` from clean state = full env; `terraform destroy` removes everything except bootstrap.
- [ ] All images pulled from ACR by managed identity (ACR admin disabled).
- [ ] KEDA ScaledJob scales from 0 on queue depth and returns to 0 (recorded via `kubectl get pods -w` + metrics).
- [ ] Container Apps query service: 0 replicas after 5 min idle (metric screenshot).
- [ ] VM runs only during its window (activity log evidence).
- [ ] `make park` / `make unpark` work and are idempotent.
