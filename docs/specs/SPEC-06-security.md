# SPEC-06 — Security (Entra ID, RBAC, Key Vault, network)

Owner agents: `infra-engineer`, `api-engineer`; reviewer: `security-reviewer` · Tasks: T-401 … T-406

## 1. Identity
- **Entra ID app registration** `tidal-api` exposes `api://tidal` with **app roles**:
  `Tidal.Reader`, `Tidal.Trader`, `Tidal.Quant`, `Tidal.Admin`. Roles are assigned to **groups**, not
  users. A second registration `tidal-cli` (public client, device code) for demos.
- App registrations are managed with the Terraform `azuread` provider.
- Every workload has a **user-assigned managed identity**: `id-api`, `id-query`, `id-worker`,
  `id-ingest`, `id-tiering`, `id-legacy-vm`. AKS uses **workload identity** (federated credential per
  service account).
- CI uses **GitHub OIDC** federated credentials: `repo:<owner>/tidal:environment:dev` → `sp-tidal-ci`.

## 2. Azure RBAC matrix (least privilege, scoped to resource not RG)

| Identity | Storage (md, reference) | Storage (backtests) | SQL | Service Bus | Key Vault | ACR |
|---|---|---|---|---|---|---|
| id-api | Blob Data Reader | Blob Data Reader | db_datareader + exec on `ops` procs | Data Sender (`backtest-jobs`, `tiering-jobs`), Data Receiver (`backtest-results`) | Secrets User | — |
| id-query | Blob Data Reader | — | db_datareader | — | Secrets User | — |
| id-worker | Blob Data Reader | Blob Data Contributor | db_datareader on `md`, `ref` (unified reader reads the hot window) | Data Receiver (`backtest-jobs`), Data Sender (`backtest-results`) | — | — |
| id-ingest | Blob Data Contributor (`raw`,`marketdata`,`reference`) | — | db_datareader + db_datawriter on `md`, `ref`, `ops` (checkpoints, instrument first/last ts) | — | — | — |
| id-tiering | Blob Data Owner on container `marketdata` (blob index tags need `tags/write`; the ARM "Tag Contributor" role does **not** cover blob tags; T-403 checks whether Blob Data Contributor + a custom `tags/write` role is tighter) | — | db_datareader/writer + delete on `md`, `ops` | Data Receiver (`tiering-jobs`) | — | — |
| id-legacy-vm | Blob Data Contributor (`exports`) | — | db_datareader | — | — | — (plus custom role `vm-self-deallocate`: `deallocate/action` on its own VM only) |
| AKS kubelet | — | — | — | — | — | AcrPull |
| KEDA operator (workload identity) | — | — | — | Data Owner on `backtest-jobs` (queue-length reads need management rights; T-403 checks whether a narrower custom role works) | — | — |
| sp-tidal-ci | — | — | — | — | — | AcrPush; Contributor on RG `rg-tidal-dev` only (enough for image push and app updates; **not** for `terraform apply`, which needs role-assignment and Graph rights and stays human-only) |

SQL: Entra-only authentication (`azuread_authentication_only = true`), identities mapped with
`CREATE USER [id-api] FROM EXTERNAL PROVIDER`. No SQL logins.

## 3. Secrets
- Key Vault (RBAC mode, purge protection on, soft delete 7 d) holds only the App Insights connection
  string (no vendor keys since ADR-0013).
- App Service / Container Apps read via Key Vault references; AKS via Secrets Store CSI driver.
- `gitleaks` in pre-commit and CI; `TIDAL_SQL__PASSWORD` only exists in `.env.local`.

## 4. Network (Phase 4, toggled by `enable_private_networking`)
- VNet with subnets: `snet-aks`, `snet-aca` (Container Apps env, workload profiles), `snet-app`
  (App Service VNet integration), `snet-pe` (private endpoints), `snet-vm`.
- Private endpoints + private DNS zones for Blob, SQL, Key Vault, Service Bus (**Premium only** for SB PE →
  documented: we keep SB public with RBAC + `minimum_tls_version 1.2` to avoid Premium cost; ADR-0006).
- Storage: `public_network_access_enabled = false` when flag on; `allow_nested_items_to_be_public = false`;
  shared key access **disabled** (`shared_access_key_enabled = false`) → forces Entra auth everywhere.
- VM: no public IP; access via **Azure Bastion Developer** (free tier; check region availability) or `az ssh vm` with Entra login.

## 5. Supply chain
- Images: multi-stage, non-root user, read-only root FS, pinned base digest.
- CI: `trivy image` (fail on HIGH/CRITICAL with fix available), SBOM (syft) attached to release.
- ACR: Basic SKU, admin user **disabled**, pulls only via managed identity.

## 6. Threat model
STRIDE table in `docs/adr/0008-threat-model.md` (owned by `security-reviewer`).

## 7. Acceptance checklist (security-reviewer sign-off)
- [ ] Zero secrets in repo, image layers, app settings (except KV references).
- [ ] Each identity has exactly the roles in §2 (`az role assignment list` export diffed in CI).
- [ ] Shared key access disabled on storage; SQL Entra-only.
- [ ] Role tests: Reader cannot POST backtests (403); Trader cannot call admin (403).
- [ ] Trivy clean; non-root containers; no `privileged`/`hostPath` in Helm chart (kube-linter/kubeconform).
- [ ] Threat model reviewed.
