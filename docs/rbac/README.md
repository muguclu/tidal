# RBAC matrix, drift check and apply prerequisites (T-403)

Source of truth: SPEC-06 section 2. Code: `infra/terraform/envs/dev/rbac.tf` (one row per assignment, each with a
justification that becomes the assignment description) on top of the RBAC engine in `infra/terraform/modules/identity/rbac.tf`.
Drift: `scripts/rbac-export.sh` against `docs/rbac/expected.json` (CI job `rbac-drift` in `.github/workflows/iac.yml`,
nightly and manual, not a required check).

## Where each SPEC-06 row lives (exactly once)
| Row | Terraform |
|---|---|
| id-api, id-query, id-worker, id-ingest, id-tiering blob, Service Bus, Key Vault | `envs/dev/rbac.tf` (`rbac_rows`) |
| AcrPull for id-api (while `enable_app_service`), id-query, id-ingest, id-tiering | `envs/dev/apps.tf` `apps_acr_pull` |
| AKS kubelet AcrPull | `envs/dev/aks.tf` `aks_kubelet_acr_pull` |
| id-legacy-vm (blob, self-deallocate) and the Logic App start role | `envs/dev/vm.tf`, `modules/vm_legacy` (T-310) |
| KEDA operator (`id-keda-operator`, federated credential, Data Owner on `backtest-jobs`) | `envs/dev/rbac.tf` + `module.rbac` |
| Tiering scaler: `id-tiering` Data Owner on `tiering-jobs` | `envs/dev/rbac.tf` (`tiering_queue_role`) |
| sp-tidal-ci (AcrPush) | `envs/dev/rbac.tf` (principal looked up with `ci_client_id`) |
| sp-tidal-ci-plan (PR plan app, credentials, Reader, state Blob Reader, Graph Application.Read.All) and sp-tidal-rbac-drift (drift app, custom subscription read role, Graph Application.Read.All); T-408 | `infra/terraform/bootstrap/main.tf` (human-applied, local state) |
| sp-tidal-deploy (deploy-only app and roles) | `envs/dev/rbac.tf` (needs `github_repo_path`) |
| Entra app roles (`Tidal.Query.Read`) | `envs/dev/entra.tf` (T-401) |
| SQL roles | `docs/rbac/sql-grants.sql` (T-SQL, not ARM) |

## Decisions and deviations
- Custom role for blob tags: not adopted, `id-tiering` keeps Storage Blob Data Owner on `marketdata`. Verify live
  (Contributor + `blobs/tags/write` + `blobs/filter/action`) before narrowing.
- Tiering scaler identity: Container Apps runs the KEDA scale rule as the job identity (one identity per job in
  `modules/container_apps`), so `id-tiering` itself is the scaler identity. While `enable_tiering_event_job` is true its role on
  `tiering-jobs` is Data Owner instead of Data Receiver (one assignment either way). A separate scaler identity needs a
  `modules/container_apps` change; do it if Owner on `id-tiering` is judged too wide (it can also send to that queue).
- `sp-tidal-ci` lost the RG-wide Contributor of the first SPEC draft; it only pushes images. Deploys use `sp-tidal-deploy`.
- Key Vault: `cursor-hmac-key` (ephemeral `random_bytes`, 32 bytes, base64) and `appinsights-connection-string` are created through the ARM API
  (`azapi_resource`, `Microsoft.KeyVault/vaults/secrets`) with write-only `sensitive_body`: **neither value is stored in Terraform state or plan**.
  Because ARM never returns secret values, the Reader-only plan identity can refresh them (an azurerm secret would need a data-plane read).
  Rotate the cursor key by raising `cursor_hmac_version`; running API instances must restart to pick it up.
  Requires Terraform >= 1.11 (write-only attributes).
- `TIDAL_OTEL__CONNECTION_STRING` is still not wired in `apps.tf` (follow-up: Key Vault reference to the secret above).

## Human prerequisites for `terraform apply`
1. Applier is Owner, or Contributor plus User Access Administrator, on the subscription (role assignments, custom roles).
2. Entra: Application Administrator (or Graph `Application.ReadWrite.All` + `AppRoleAssignment.ReadWrite.All`) for `sp-tidal-deploy`.
3. Nothing extra for the secrets: the applier writes them through ARM (`Microsoft.KeyVault/vaults/secrets/write`, included in Owner/Contributor), no Key Vault data-plane role.
4. tfvars: `ci_client_id` (output of `bootstrap`), `github_repo_path` (`owner/repo`); `deploy_github_environment` defaults to `dev-deploy`. Create that GitHub environment first (main only, required reviewer; docs/runbooks/deploy.md). Re-apply `bootstrap` once to remove the old `ci_state_blob` role from sp-tidal-ci.
5. After apply: run `docs/rbac/sql-grants.sql` as the SQL Entra admin; set GitHub environment secrets (`gh secret set --env dev-deploy`)
   `TIDAL_KEDA_CLIENT_ID` (output `keda_identity`) on the `dev-deploy` environment, and set that environment's own `AZURE_CLIENT_ID` to the output
   `deploy_client_id` (`deploy.yml` runs in `dev-deploy`; `sp-tidal-ci` keeps the `dev` environment).
6. AKS: the human applier creates namespace `tidal` (`kubectl create namespace tidal`) before the namespace-scoped RBAC Writer
   assignment; RBAC Writer cannot create it and `deploy.yml` does not pass `--create-namespace`.

## Drift check
```bash
az login   # Reader on the subscription + Graph Application.Read.All
GITHUB_REPOSITORY=<owner>/<repo> scripts/rbac-export.sh --check docs/rbac/expected.json
```
The nightly job uses the read-only app `sp-tidal-rbac-drift` (environment `rbac-drift`, restricted to main; environment secret `AZURE_RBAC_DRIFT_CLIENT_ID`; created by `bootstrap` since T-408). It holds the custom role `tidal-rbac-drift-reader` (reads only, no state access) at subscription scope and Graph `Application.Read.All`. Run by hand, `az login` needs the same. `sp-tidal-ci-plan` (PR plans) is covered by the same check. The Graph grants sit on the Microsoft Graph service principal and are not part of the export. The export also lists assignments of known Tidal principals at any scope (not only inside the resource group). Humans (`<user>`) and groups (`<group>`) are normalized; secrets never appear. Update
`expected.json` in the same PR as any intentional change (regenerate with `scripts/rbac-export.sh` and review the diff).
Known limit: the SQL roles and Kubernetes RBAC inside the cluster are not exported.
