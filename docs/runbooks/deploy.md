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
Because the run's ref is `main`, the `dev` environment must allow `main` (already required by `docs/runbooks/images.md`).
The chart and smoke tests are checked out at the tag's commit.

Never trusted: a bare tag. Everything is deployed as `<acr>.azurecr.io/tidal@sha256:...` (App Service, Container Apps,
Helm `image.digest`). The digest and UTC time are in the job summary.

## Configuration source (no secrets, no Terraform in CI)
The workflow reads **environment variables of the GitHub environment `dev`** that a human sets once after
`terraform apply` (never from CI). Run in `infra/terraform/envs/dev`:

```bash
terraform output -json tidal_config | jq -r 'to_entries[] | "\(.key)=\(.value)"'   # endpoints, client ids
terraform output -raw resource_group_name; terraform output -raw aks_name
terraform output -raw api_hostname; terraform output -json apps_job_names
```

| Variable | Source |
|---|---|
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME` | as for `images.yml` |
| `TIDAL_RESOURCE_GROUP` | output `resource_group_name` |
| `TIDAL_WEBAPP_NAME` | web app name, `app-tidal-api-dev-<suffix>` (output `api_hostname` minus `.azurewebsites.net`) |
| `TIDAL_API_BASE_URL` | `https://<api_hostname>` |
| `TIDAL_QUERY_APP_NAME` | `ca-tidal-query` |
| `TIDAL_QUERY_BASE_URL` | `https://<query FQDN>` (module output `query_fqdn`, same value as `TIDAL_QUERY__BASE_URL`) |
| `TIDAL_JOB_NAMES` | space separated values of output `apps_job_names` (`caj-tidal-...`) |
| `TIDAL_AKS_NAME` | output `aks_name` |
| `TIDAL_WORKER_CLIENT_ID` | `IDENTITY_CLIENT_ID_WORKER` |
| `TIDAL_KEDA_CLIENT_ID` | KEDA operator identity client id (created by T-403) |
| `TIDAL_STORAGE_URL`, `TIDAL_SQL_SERVER`, `TIDAL_SERVICEBUS_FQDN` | `TIDAL_STORAGE__ACCOUNT_URL`, `TIDAL_SQL__SERVER`, `TIDAL_SERVICEBUS__FQDN` |

Every value is format-validated in the first step; a missing or malformed one fails the run before any login.

## Permissions the OIDC app needs (to be granted in Terraform, T-403)
AcrPull/Reader on the ACR (resolve digests); Website Contributor on the web app; Container Apps contributor on the
Container Apps resource group; `Azure Kubernetes Service Cluster User Role` plus RBAC Writer/Admin on namespace `tidal`
(local accounts are disabled; kubelogin uses the `azurecli` login). Not granted today: until it is, the matching step fails with 403.

## Behaviour and limits
- App Service health is polled from the runner (`/v1/health/live`, up to 4 min). If the app has an IP allow-list
  (`apps.tf`), the runner IPs must be allowed or that step cannot succeed; the same applies to the smoke job.
- AKS is cost-parked by design. If its power state is not `Running` the Helm step is skipped with a warning and noted in
  the summary; start it (`az aks start`) and re-run via `workflow_dispatch`. Helm runs with `--atomic --wait`.
- `az containerapp` uses the dynamically installed CLI extension (not version pinned).
- The smoke job has no Azure credentials (`id-token` not granted). Concurrency group `deploy-dev` queues deploys and never
  cancels a running one.

## Smoke tests (`tests/smoke`)
`uv run pytest tests/smoke -m smoke` with `TIDAL_SMOKE_API_URL` and `TIDAL_SMOKE_QUERY_URL` (https). Checks: API `/v1/health/live`
and `/ready`, query `/health/live`. The backtest-submit check (202) **skips** until T-402 (Entra auth) exists and
`TIDAL_SMOKE_TOKEN` + `TIDAL_SMOKE_BACKTEST_JSON` are provided. Without URLs every test skips, so the normal suites are unaffected.
The `smoke` marker is registered in `tests/smoke/conftest.py` (pyproject unchanged).

## Rollback
Run the workflow manually with the previous digest (from the earlier run summary or `az acr repository show-manifests`).
