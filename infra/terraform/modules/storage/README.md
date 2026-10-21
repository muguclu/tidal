# storage

One StorageV2 account (Standard LRS, Hot, **flat namespace**) with the six containers of the storage-layout
contract: `raw`, `marketdata` (ADR-0010), `reference`, `backtests`, `exports`, `bench`. Flat namespace keeps blob index
tags usable for lifecycle (ADR-0004).

- Security: shared key disabled, OAuth default, TLS 1.2, HTTPS only, no nested public items, no local users, private containers.
- `enable_private_networking = false` (default) leaves the public endpoint on: access is Entra ID + RBAC only. `true` disables public access; a private endpoint is a Phase 4 task.
- Versioning off (immutable Parquet; restatements are new parts). Blob and container soft delete 7 d.
- **Provider requirement:** with shared key disabled, the azurerm provider in the env composition must set `storage_use_azuread = true`, and the identity running Terraform needs a data-plane role such as `Storage Blob Data Owner` on the account.
- Lifecycle management policy is T-207 (not here). It needs `id` for the account and `container_names` for `prefix_match = ["<container>/..."]`; rules filter on the `age_band` blob index tag.
- RBAC (T-403): scope role assignments to `container_ids["<name>"]`, per SPEC-06 section 2.
- `prevent_destroy` is set on the account and containers; removing the module requires editing the lifecycle blocks first.
- Checkov skips (inline, justified): LRS (CKV_AZURE_206), public endpoint while flag off (CKV_AZURE_59), no CMK (CKV2_AZURE_1), no private endpoint (CKV2_AZURE_33), queue logging (CKV_AZURE_33), read logging until T-506 (CKV2_AZURE_21, per container).

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
| `azurerm_storage_account.this` | resource |
| `azurerm_storage_container.this` | resource (for_each) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `name` | Storage account name, 3-24 lowercase letters and digits. | `string` | n/a | yes |
| `resource_group_name` | Resource group for the account. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `enable_private_networking` | Disable public network access (Phase 4 private endpoint). | `bool` | `false` | no |
| `containers` | Container names (3-63 chars). | `set(string)` | six contract containers | no |
| `soft_delete_days` | Blob and container soft delete retention. | `number` | `7` | no |
| `tags` | Tag map from `module.resource_group.tags` (validated). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags`. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `id` | Storage account ID. |
| `name` | Storage account name. |
| `primary_blob_endpoint` | Primary blob endpoint. |
| `container_ids` | Map of container name to ID (RBAC scoping, T-403). |
| `container_names` | Sorted container names (lifecycle prefixes, T-207). |
<!-- END_TF_DOCS -->
