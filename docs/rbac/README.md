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
- Key Vault: `cursor-hmac-key` (random 32 bytes, base64) and `appinsights-connection-string` are created in Terraform. **Both values are in
  Terraform state** (private tfstate container, Entra-only). Rotate the cursor key with
  `terraform apply -replace=random_bytes.cursor_hmac`; running API instances must restart to pick it up.
- `TIDAL_OTEL__CONNECTION_STRING` is still not wired in `apps.tf` (follow-up: Key Vault reference to the secret above).

## Human prerequisites for `terraform apply`
1. Applier is Owner, or Contributor plus User Access Administrator, on the subscription (role assignments, custom roles).
2. Entra: Application Administrator (or Graph `Application.ReadWrite.All` + `AppRoleAssignment.ReadWrite.All`) for `sp-tidal-deploy`.
3. Key Vault Secrets Officer on the new vault for the applier (data plane; Owner is not enough), granted out of band.
4. tfvars: `ci_client_id` (output of `bootstrap`), `github_repo_path` (`owner/repo`), optional `deploy_github_environment`.
5. After apply: run `docs/rbac/sql-grants.sql` as the SQL Entra admin; set GitHub environment variables
   `TIDAL_KEDA_CLIENT_ID` (output `keda_identity`) and, to use the deploy-only identity, point `deploy.yml` at
   `output deploy_client_id` (its `AZURE_CLIENT_ID` currently resolves to the `sp-tidal-ci` app; `deploy.yml` change belongs to T-311).
6. AKS: namespace `tidal` must exist before the namespace-scoped RBAC Writer assignment (the Helm release creates it; if the
   assignment fails on a missing namespace, apply once after the first `helm upgrade --install --create-namespace`).

## Drift check
```bash
az login   # Reader on the subscription + Graph Application.Read.All
GITHUB_REPOSITORY=<owner>/<repo> scripts/rbac-export.sh --check docs/rbac/expected.json
```
CI uses the read-only `sp-tidal-ci-plan` app (repo variable `AZURE_PLAN_CLIENT_ID`); it needs Graph `Application.Read.All` in
addition to Reader. Humans (`<user>`) and groups (`<group>`) are normalized; secrets never appear. Update
`expected.json` in the same PR as any intentional change (regenerate with `scripts/rbac-export.sh` and review the diff).
Known limit: the SQL roles and Kubernetes RBAC inside the cluster are not exported.
