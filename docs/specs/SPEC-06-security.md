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
- CI uses **GitHub OIDC** federated credentials, one subject per app (identity isolation, T-403):
  `repo:<owner>/tidal:environment:dev` → `sp-tidal-ci` (image push, AcrPush only) and
  `repo:<owner>/tidal:environment:dev-deploy` → `sp-tidal-deploy` (deploy; the environment allows `main` only and
  requires a reviewer, `docs/runbooks/deploy.md`). Neither app has a second subject, a client secret or a certificate. The read-only `sp-tidal-ci-plan` (T-408) is the exception that may use `repo:<owner>/tidal:pull_request` (plus `repo:<owner>/tidal:ref:refs/heads/main` for the push plan and nightly drift), because it holds no write role.

## 2. Azure RBAC matrix (least privilege, scoped to resource not RG)

| Identity | Storage (md, reference) | Storage (backtests) | SQL | Service Bus | Key Vault | ACR |
|---|---|---|---|---|---|---|
| id-api | Blob Data Reader | Blob Data Reader | db_datareader + exec on `ops` procs | Data Sender (`backtest-jobs`, `tiering-jobs`), Data Receiver (`backtest-results`) | Secrets User on secrets `cursor-hmac-key` and `appinsights-connection-string` | AcrPull on the registry (only while `enable_app_service` is true) |
| id-query | Blob Data Reader | — | db_datareader | — | Secrets User on secret `appinsights-connection-string` | AcrPull on the registry |
| id-worker | Blob Data Reader | Blob Data Contributor | calls tidal-query (app role/audience per T-402), no SQL access (ADR-0016) | Data Receiver (`backtest-jobs`), Data Sender (`backtest-results`) | — | — |
| id-ingest | Blob Data Contributor (`raw`,`marketdata`,`reference`) | — | db_datareader + db_datawriter on `md`, `ref`, `ops` (checkpoints, instrument first/last ts) | — | — | AcrPull on the registry |
| id-tiering | Blob Data Owner on container `marketdata` (blob index tags need `tags/write`; the ARM "Tag Contributor" role does **not** cover blob tags. T-403 kept Data Owner: a custom `tags/write` role on top of Blob Data Contributor was not adopted because it cannot be verified without Azure and Find Blobs by Tags may need a further `filter/action`; revisit with a live test) | — | db_datareader/writer (writer includes delete) | Data Receiver (`tiering-jobs`); **Data Owner on that one queue while `enable_tiering_event_job` is true**: the Container Apps job identity doubles as the KEDA scale-rule identity and queue counts need Manage rights | — | AcrPull on the registry |
| id-legacy-vm | Blob Data Contributor (`exports`), Blob Data Reader (`marketdata`) | — | — | — | — | — (plus custom role `vm-self-deallocate`: `deallocate/action` on its own VM only) |
| AKS kubelet | — | — | — | — | — | AcrPull |
| KEDA operator (workload identity) | — | — | — | Data Owner on `backtest-jobs` only (queue-length reads need management rights; identity `id-keda-operator`, federated to `system:serviceaccount:kube-system:keda-operator`; a narrower custom role was not adopted: the Service Bus data actions KEDA calls are not documented as separable) | — | — |
| sp-tidal-ci | — | — | — | — | — | AcrPush on the registry **only**. The RG-wide Contributor was dropped in T-403: app updates move to `sp-tidal-deploy` (next row), so the image-push identity cannot change infrastructure |
| sp-tidal-ci-plan (T-408) | — | — | — | — | — | No ACR role. Read-only: Reader on the dev resource group, Storage Blob Data Reader on the `tfstate` container, Microsoft Graph `Application.Read.All` (application permission). No write role. Created by `infra/terraform/bootstrap` |
| sp-tidal-deploy (T-403) | — | — | — | — | — | AcrPull on the registry. Other scopes: Website Contributor on the web app; Contributor on `ca-tidal-query` and on each Container Apps Job; Reader + Cluster User Role on the AKS cluster and RBAC Writer on namespace `tidal`; Managed Identity Operator on the identities of those targets. Never `terraform apply` (needs role-assignment and Graph rights, human-only) |

`sp-tidal-deploy` effectively holds the union of the rights of its target identities (`id-api`, `id-query`, `id-ingest`,
`id-tiering`) through Managed Identity Operator: a resource it may update can be made to run as them. **Verify live; drop
the assignment where image-only updates do not need it.** The namespace `tidal` is created by the human applier (RBAC Writer cannot).

SQL: Entra-only authentication (`azuread_authentication_only = true`), identities mapped with
`CREATE USER [id-api] FROM EXTERNAL PROVIDER`. No SQL logins. The SQL roles are database objects, not Azure RBAC:
`docs/rbac/sql-grants.sql` (run once by the SQL Entra admin) is their source; the drift export cannot see them.
`db_datareader`/`db_datawriter` are database-wide, which equals "md, ref, ops" while those are the only schemas.

## 3. Secrets
- Key Vault (RBAC mode, purge protection on, soft delete 7 d) holds the App Insights connection
  string and `cursor-hmac-key` (T-402/T-403: base64 of 32+ random bytes, generated by an ephemeral resource and
  written through the ARM API as a write-only value, so it is never in Terraform state or plans; readable by `id-api`
  with Secrets User; an optional `cursor-hmac-key-previous` is verify-only during rotation). No vendor keys since ADR-0013.
- App Service / Container Apps read via Key Vault references; AKS via Secrets Store CSI driver.
- `gitleaks` in pre-commit and CI; `TIDAL_SQL__PASSWORD` only exists in git-ignored `.env`/`.env.local`; the sole exception is an ephemeral, per-run, masked SA password generated inside the CI integration job (T-128).

## 4. Network (Phase 4, toggled by `enable_private_networking`)
- VNet with subnets: `snet-aks`, `snet-aca` (Container Apps env, Consumption-only, `/23`, no workload profiles), `snet-app`
  (App Service VNet integration), `snet-pe` (private endpoints), `snet-vm`.
- Private endpoints + private DNS zones for Blob, SQL, Key Vault, Service Bus (**Premium only** for SB PE →
  documented: we keep SB public with RBAC + `minimum_tls_version 1.2` to avoid Premium cost; ADR-0006).
- Storage: `public_network_access_enabled = false` when flag on; `allow_nested_items_to_be_public = false`;
  shared key access **disabled** (`shared_access_key_enabled = false`) → forces Entra auth everywhere.
- VM: no inbound path (both NSGs deny all inbound, no listener, no extensions); no interactive access by design; logs via
  boot diagnostics / serial console. Dev deviation (ADR-0018): with the flag on the NIC carries one outbound-only Standard
  public IP instead of a NAT gateway (`network_vm_outbound = "nat_gateway"` restores a VM without a public address).
  Production uses NAT or a firewall.

## 5. Supply chain
- Images: multi-stage, non-root user, read-only root FS, pinned base digest.
- CI: `trivy image` (fail on HIGH/CRITICAL with fix available), SBOM (syft) attached to release.
- ACR: Basic SKU, admin user **disabled**, pulls only via managed identity.

## 6. Threat model
STRIDE table in `docs/adr/0008-threat-model.md` (owned by `security-reviewer`).

## 7. Acceptance checklist (security-reviewer sign-off)
Signed in T-406 for **dev** (2026-10-24, `main` at `e848c39`). Evidence per item: `docs/status/2026-W43-G4.md`; residual
risks: `docs/adr/0008-threat-model.md`. Only items that are PASS are ticked.
- [x] Zero secrets in repo, image layers, app settings (except KV references). **PASS**: gitleaks v8.24.3 over full history
  (264 commits) found no leaks; app-setting validation in `modules/{app_service,container_apps}/variables.tf`; trivy secret scan.
- [ ] Each identity has exactly the roles in §2 (`scripts/rbac-export.sh`: role assignments, Entra app role assignments and app credentials, diffed against `docs/rbac/expected.json` by the nightly, non-required `rbac-drift` job in `iac.yml`).
  **NEEDS-CLOUD**: IaC matches §2; the diff needs a live tenant; `sp-tidal-ci-plan` is in `bootstrap` IaC and in the baseline since T-408 (ADR-0008 R-18), pending import of the hand-made app.
- [x] Shared key access disabled on storage; SQL Entra-only. **PASS**: `modules/storage/main.tf:27`, `bootstrap/main.tf:51`,
  `modules/sql/main.tf:33`.
- [ ] Role tests: Reader cannot POST backtests (403); Trader cannot call admin (403). **PASS (dev, with caveat)**: Reader 403
  and Trader 403 on `POST /v1/backtests` pass (`tests/unit/api/test_auth.py`, 43 passed); `/v1/admin/*` is not implemented,
  so the admin half is only proven by `has_role` unit tests (ADR-0008 R-20).
- [x] Trivy clean; non-root containers; no `privileged`/`hostPath` in Helm chart (kube-linter/kubeconform). **PASS**: trivy vuln
  and secret scans passed on the tree of `e848c39` (`images` run 40118624869); `USER 10001`; kubeconform 7/7 valid and kube-linter clean on the rendered chart (run by hand, not in
  CI: ADR-0008 R-19).
- [x] Threat model reviewed. **PASS**: `docs/adr/0008-threat-model.md`.
