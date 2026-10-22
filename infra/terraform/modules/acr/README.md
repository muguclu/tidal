# acr

One Azure Container Registry (**Basic**, `acrtidal<env><suffix>`) plus a weekly `acr purge` ACR Task (SPEC-07 section 2, SPEC-09 C9).

- Security: `admin_enabled = false`, `anonymous_pull_enabled = false`; pulls and pushes are Entra ID + RBAC only (SPEC-06 sections 2 and 5). `AcrPull` (AKS kubelet, apps) and `AcrPush` (CI) role assignments are T-403 and T-306, scoped to the `id` output; they are not in this module.
- Public network access stays on: Basic does not support private endpoints or network rules (Premium only).
- Retention policy, content trust, geo-replication, zone redundancy, quarantine, dedicated data endpoints and CMK are Premium-only and are deliberately not set; the purge task replaces the retention policy.
- Purge task `weekly-purge`: timer trigger (default `0 3 * * 0`, Sunday 03:00 UTC). No identity and no credentials: `{{.Run.Registry}}` tasks authenticate with the run token. Two steps, scoped to `purge_repository_regex` (default `^tidal.*$`, anchored; validation rejects anything not starting with `^tidal`):
  - Step 1: `acr purge --registry {{.Run.Registry}} --filter '^tidal.*$:^$' --untagged --ago 7d`. The tag regex `^$` matches no tag, so only untagged manifests are deleted (`--filter` selects tags to delete; `--untagged` adds manifests left without tags). `--untagged-only` would express this directly but its availability in the ACR Tasks acr-cli is unverified.
  - Step 2: `acr purge --registry {{.Run.Registry}} --filter '^tidal.*$:^sha-.*$' --untagged --ago 30d`.
  - A `check` block fails the plan if a tag filter ever becomes `.*`.

  | Artifact in a `tidal*` repo | Age | Step 1 | Step 2 | Result |
  |---|---|---|---|---|
  | tag `v1.2.3` | any | no match | no match | kept forever |
  | tag `latest` or `tidal-*` | any | no match | no match | kept |
  | tag `sha-abc` | 10 d | no match | not old enough | kept |
  | tag `sha-abc` | over 30 d | no match | tag and manifest deleted | deleted |
  | untagged manifest | over 7 d | deleted | (deleted if older than 30 d) | deleted |
  | untagged manifest | 3 d | too young | too young | kept |
  | any tag in a repo not matching `^tidal.*$` | any | out of scope | out of scope | kept |
- First-apply step (C9): after the first apply on dev, run `az acr task run --name weekly-purge --registry <name>` once. Record `az acr show-usage -r <name>` (Size) before and after, and the manifest count, in `docs/finops/results/`.
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

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group for the registry. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `name_suffix` | 3 to 8 lowercase alphanumeric characters for global uniqueness. | `string` | n/a | yes |
| `purge_schedule` | Five-field cron (UTC) for the purge task. | `string` | `"0 3 * * 0"` | no |
x
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
