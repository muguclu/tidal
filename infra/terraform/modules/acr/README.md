# acr

One Azure Container Registry (**Basic**, `acrtidal<env><suffix>`) plus a weekly `acr purge` ACR Task (SPEC-07 section 2, SPEC-09 C9).

- Security: `admin_enabled = false`, `anonymous_pull_enabled = false`; pulls and pushes are Entra ID + RBAC only (SPEC-06 sections 2 and 5). `AcrPull` (AKS kubelet, apps) and `AcrPush` (CI) role assignments are T-403 and T-306, scoped to the `id` output; they are not in this module.
- Public network access stays on: Basic does not support private endpoints or network rules (Premium only).
- Retention policy, content trust, geo-replication, zone redundancy, quarantine, dedicated data endpoints and CMK are Premium-only and are deliberately not set; the purge task replaces the retention policy.
- Purge task `weekly-purge`: timer trigger (default `0 3 * * 0`, Sunday 03:00 UTC), system-assigned identity (no credentials), `AcrDelete` on this registry only. Step 1 deletes untagged manifests older than `untagged_retention_days` (7); step 2 deletes `sha-*` tags older than `sha_tag_retention_days` (30). Scope is `purge_repository_regex` (default `tidal.*`). `v*` release tags never match, so they are kept forever.
- `login_server` feeds `images.yml` and the Helm image repository.
- No `prevent_destroy`: `terraform destroy` must remove everything except bootstrap (SPEC-07 section 6).
- Cost: see `docs/finops/deltas/T-122.md` (Basic unit 0.1466 EUR/day, 10 GiB included).

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_container_registry.this` | resource |
| `azurerm_container_registry_task.purge` | resource |
| `azurerm_role_assignment.purge_task_acr_delete` | resource (AcrDelete, scope = the registry) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group for the registry. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `name_suffix` | 3 to 8 lowercase alphanumeric characters for global uniqueness. | `string` | n/a | yes |
| `purge_schedule` | Five-field cron (UTC) for the purge task. | `string` | `"0 3 * * 0"` | no |
| `purge_repository_regex` | Repositories the purge touches. | `string` | `"tidal.*"` | no |
| `untagged_retention_days` | Age in days for untagged manifests. | `number` | `7` | no |
| `sha_tag_retention_days` | Age in days for `sha-*` tags. | `number` | `30` | no |
| `tags` | Tag map from `module.resource_group.tags`. | `map(string)` | n/a | yes |
| `component` | Value of the component tag. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `id` | Registry resource ID. |
| `name` | Registry name. |
| `login_server` | Registry login server. |
| `purge_task_id` | Purge task resource ID. |
<!-- END_TF_DOCS -->
