# bootstrap

Applied **once, by a human, with local state** (SPEC-07 section 1). Creates the Terraform state storage
and the GitHub OIDC CI identity. See `docs/runbooks/bootstrap.md` for the procedure.

| Resource | Purpose |
|---|---|
| `rg-tidal-tfstate` | Resource group, survives `terraform destroy` of any env |
| Storage account (StorageV2, LRS, Hot) | Shared key disabled, TLS 1.2, no public blob, versioning, 7 d soft delete (blobs and containers) |
| Container `tfstate` | Remote state for `envs/*` |
| Entra app + SP `sp-tidal-ci` | CI identity, no client secret |
| Federated credential | `repo:<owner>/<repo>:environment:<env>` only. No `pull_request` credential (it would bypass environment protection); T-127 adds a separate app `sp-tidal-ci-plan` with Reader + Storage Blob Data Reader for PR plans |
| Management lock + `prevent_destroy` | `CanNotDelete` on the state account (see runbook Rollback) |
| Lifecycle rule | Delete noncurrent blob versions after 30 days |
| Role assignment | Storage Blob Data Contributor on the `tfstate` container only |

Later tasks add AcrPush and Contributor on `rg-tidal-dev` (SPEC-06 section 2). `terraform apply` for envs stays human-only.

Provider requirements: Terraform >= 1.9, azurerm ~> 4.0, azuread ~> 3.0.

<!-- BEGIN_TF_DOCS (hand-maintained until terraform-docs is wired into CI) -->
## Inputs

| Name | Description | Default |
|---|---|---|
| `subscription_id` | Azure subscription GUID (explicit) | required |
| `state_account_name` | Globally unique state account name, 3-24 lowercase alphanumerics | required |
| `owner` | `owner` tag | required |
| `location` | Region | `westeurope` |
| `env` | `dev` or `prod`; tag, state key and GitHub environment | `dev` |
| `cost_center` | `cost_center` tag | `portfolio` |
| `park` | `park` tag | `false` |
| `github_owner` | GitHub owner trusted by OIDC | `muguclu` |
| `github_repo` | GitHub repository trusted by OIDC | `tidal` |
| `state_soft_delete_days` | Soft delete retention (1-365) | `7` |

## Outputs

`tenant_id`, `subscription_id`, `ci_client_id`, `ci_service_principal_object_id`,
`state_resource_group_name`, `state_storage_account_name`, `state_container_name`, `backend_config`.

## Accepted checkov skips (inline in `main.tf`)

LRS only (`CKV_AZURE_206`), Microsoft-managed keys (`CKV2_AZURE_1`), public network access (`CKV_AZURE_59`; GitHub-hosted runner IPs cannot be allow-listed, access is Entra-only), no private endpoint in dev, tracked behind the Phase 4 flag (`CKV2_AZURE_33`), no queue service (`CKV_AZURE_33`),
no blob read logging before Log Analytics exists (`CKV2_AZURE_21`).
<!-- END_TF_DOCS -->
