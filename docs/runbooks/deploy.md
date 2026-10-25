# Runbook: deploy to dev (T-311)

`.github/workflows/deploy.yml` rolls one image **digest** to every compute target of the dev environment:
App Service (`tidal-api`), Container App `tidal-query`, the Container Apps Jobs, and the AKS worker chart.
Target: tag to running environment in under 15 minutes (job timeout 15 min, Helm timeout 6 min).

## Trigger
| Trigger | Behaviour |
|---|---|
| `workflow_run` of `images`, conclusion `success`, event `push`, head branch `v*` | resolves `tidal:vX.Y.Z` in ACR, requires it to equal `tidal:sha-<head_sha[:12]>`, deploys that digest |
| `workflow_dispatch` with `digest` | digest must match `sha256:<64 hex>` and exist in the ACR; use for rollback or a manual re-run |

Why `workflow_run` and not a reusable-workflow call from `images.yml`: it keeps build and deploy permissions in separate
workflows (the build workflow never gets deploy rights, and `images.yml` stays untouched/merged), and a deploy only starts
after the whole `images` run (build, scan, push) is green. The cost is that a `workflow_run` executes from the default branch
and carries tag data as untrusted input, so the workflow re-validates everything: strict `vX.Y.Z` regex, 40-hex head sha,
tag and commit tag must resolve to the same digest (a moved tag is refused), and all of it enters shell only via env vars.
Because the run's ref is `main`, the `dev-deploy` environment must allow `main` (see the next section).
The chart and smoke tests are checked out at the tag's commit.

Never trusted: a bare tag. Everything is deployed as `<acr>.azurecr.io/tidal@sha256:...` (App Service, Container Apps,
Helm `image.digest`). The digest and UTC time are in the job summary.

## Configuration source (IDs as environment secrets, names as environment variables; no Terraform in CI)
The workflow reads **secrets and variables of the GitHub environment `dev-deploy`** that a human sets once after
`terraform apply` (never from CI). Run in `infra/terraform/envs/dev`:

```bash
terraform output -json tidal_config | jq -r 'to_entries[] | "\(.key)=\(.value)"'   # endpoints, client ids
terraform output -raw resource_group_name; terraform output -raw aks_name
terraform output -raw api_hostname; terraform output -json apps_job_names
```

IDs are **secrets** (`gh secret set --env dev-deploy`), names and URLs stay variables (public repo: `vars.*` print unmasked in logs, ADR-0008 R-29; names are deterministic from the public Terraform naming).

| Name | Source |
|---|---|
| `AZURE_CLIENT_ID` (secret) | **environment-scoped** to `dev-deploy`: output `deploy_client_id` (app `sp-tidal-deploy`). Never the `images.yml` app and never a repository-level secret |
| `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (secrets), `ACR_NAME` (variable) | as for `images.yml` |
| `TIDAL_RESOURCE_GROUP` | output `resource_group_name` |
| `TIDAL_WEBAPP_NAME` | web app name, `app-tidal-api-dev-<suffix>` (output `api_hostname` minus `.azurewebsites.net`) |
| `TIDAL_API_BASE_URL` | `https://<api_hostname>` |
| `TIDAL_QUERY_APP_NAME` | `ca-tidal-query` |
| `TIDAL_QUERY_BASE_URL` | `https://<query FQDN>` (module output `query_fqdn`, same value as `TIDAL_QUERY__BASE_URL`) |
| `TIDAL_JOB_NAMES` | space separated values of output `apps_job_names` (`caj-tidal-...`) |
| `TIDAL_AKS_NAME` | output `aks_name` |
| `TIDAL_WORKER_CLIENT_ID` (secret) | `IDENTITY_CLIENT_ID_WORKER` |
| `TIDAL_KEDA_CLIENT_ID` (secret) | output `keda_identity.client_id` (T-403) |
| `TIDAL_STORAGE_URL`, `TIDAL_SQL_SERVER`, `TIDAL_SERVICEBUS_FQDN` | `TIDAL_STORAGE__ACCOUNT_URL`, `TIDAL_SQL__SERVER`, `TIDAL_SERVICEBUS__FQDN` |

Every value is format-validated in the first step; a missing or malformed one fails the run before any login.

## GitHub environment `dev-deploy` (identity isolation, T-403)
Deploy uses its own Entra app `sp-tidal-deploy` and its own GitHub environment, so the image-build identity
(`sp-tidal-ci`, environment `dev`, AcrPush only) can never obtain deploy rights. Each app has exactly one federated subject:

| App | Subject | Roles |
|---|---|---|
| `sp-tidal-ci` | `repo:<owner>/<repo>:environment:dev` | AcrPush on the registry |
| `sp-tidal-deploy` | `repo:<owner>/<repo>:environment:dev-deploy` | see below |

Create the environment `dev-deploy` once (Settings, Environments) with: **deployment branches and tags: selected branches,
`main` only** (a `workflow_run` deploy executes from `main`), **required reviewers: at least one human** (the deploy job waits
for approval), and no environment secrets. Set the variables above on it. Do not create a `dev-deploy` variable at repository level.

Roles of `sp-tidal-deploy` (all in `infra/terraform/envs/dev/rbac.tf`, each with a justification): AcrPull on the registry;
Website Contributor on the web app; Contributor on `ca-tidal-query` and on each Container Apps Job; Reader and
`Azure Kubernetes Service Cluster User Role` on the cluster; `Azure Kubernetes Service RBAC Writer` on namespace `tidal`;
Managed Identity Operator on the identities of those targets (verify live, drop where image-only updates do not need it:
through it the deploy identity effectively holds the union of its targets' identities' rights).

### Namespace `tidal` is created by the human applier
RBAC Writer is scoped to the namespace and cannot create it, and `deploy.yml` deliberately does not pass `--create-namespace`.
Before the first deploy, and before applying the namespace-scoped role assignment, run once as a cluster admin:

```bash
az aks get-credentials --name <aks_name> --resource-group <rg>
kubectl create namespace tidal
kubectl label namespace tidal \n  pod-security.kubernetes.io/enforce=restricted \n  pod-security.kubernetes.io/audit=restricted \n  pod-security.kubernetes.io/warn=restricted
kubectl get namespace tidal --show-labels   # verify all three labels before the first deploy
```

`values-dev.yaml` therefore sets `namespace.create=false` and `externallyManagedAck=true` (R-26 in ADR-0008):
without the labels the worker would run without PSA `restricted`.

## Behaviour and limits
- App Service health is polled from the runner (`/v1/health/live`, up to 4 min). If the app has an IP allow-list
  (`apps.tf`), the runner IPs must be allowed or that step cannot succeed; the same applies to the smoke job.
- AKS is cost-parked by design. If its power state is not `Running` the Helm step is skipped with a warning and noted in
  the summary; start it (`az aks start`) and re-run via `workflow_dispatch`. Helm runs with `--atomic --wait`.
- The `containerapp` CLI extension is pinned (`0.3.55`, dynamic install off) and kubelogin is pinned (`v0.2.21` via a SHA-pinned `azure/use-kubelogin`). Bump deliberately.
- The smoke job gets its URLs from the deploy job outputs and runs with `TIDAL_SMOKE_REQUIRED=1`: an empty URL fails instead of skipping. Only the backtest-submit test may skip.
- Forked-repo `workflow_run` events are ignored (`head_repository` must equal this repository). Cleanup removes the kubeconfig and `~/.kube/cache`.
- The smoke job has no Azure credentials (`id-token` not granted). Concurrency group `deploy-dev` queues deploys and never
  cancels a running one.

## Smoke tests (`tests/smoke`)
`uv run pytest tests/smoke -m smoke` with `TIDAL_SMOKE_API_URL` and `TIDAL_SMOKE_QUERY_URL` (https). Checks: API `/v1/health/live`
and `/ready`, query `/health/live`. The backtest-submit check (202) **skips** until T-402 (Entra auth) exists and
`TIDAL_SMOKE_TOKEN` + `TIDAL_SMOKE_BACKTEST_JSON` are provided. Without URLs every test skips, so the normal suites are unaffected.
The `smoke` marker is registered in `tests/smoke/conftest.py` (pyproject unchanged).

## SQL firewall (T-610, public endpoint, `enable_private_networking = false`)
The dev SQL server has no "Allow Azure services" rule. Each caller IP gets a single-address firewall rule
(`modules/sql`, `allowed_ip_addresses`), built in `envs/dev/apps.tf` from the web app's `possible_outbound_ip_addresses`,
the Container Apps environment egress addresses (read from tidal-query, shared by tidal-query and the three jobs) and
`sql_admin_ip_addresses` (operator IPs, default empty). These addresses exist only after the apps exist and the rules are
keyed by address, so Terraform cannot plan them on a fresh environment. Apply in two steps (human applier, no `-target`):
1. `terraform apply -var-file=dev.tfvars -var sql_app_outbound_rules_enabled=false` (creates everything, no app rules).
2. `terraform apply -var-file=dev.tfvars` (default `true`; adds one rule per outbound IP).
Add your own IP via `sql_admin_ip_addresses = ["203.0.113.7"]` only for the `sql-grants.sql`/sqlcmd session, then set it
back to `[]` and re-apply the same day (home and ISP addresses get reassigned).
Later plans are single-step. Re-apply **immediately** after replacing the web app or the Container Apps environment, and
on any SQL error 40615: rules for retired addresses stay open until the next apply (fail-open window, ADR-0008 R-25).
Residual: Container Apps consumption egress addresses are not contractually stable, and the web app's possible outbound
list is large (shared with other tenants on the scale unit). If SQL starts refusing with error 40615 after a platform
change, re-apply. Stable alternatives: `enable_private_networking = true` (private endpoint, no firewall rules) or a NAT
gateway on a workload-profile environment (list-price estimate roughly 35 EUR/month with its public IP, not measured, not adopted).

## Rollback
Run the workflow manually with the previous digest (from the earlier run summary or `az acr repository show-manifests`).
