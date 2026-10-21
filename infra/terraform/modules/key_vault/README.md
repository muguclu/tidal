# key_vault

Key Vault, Standard SKU, **RBAC authorization** (no access policies), purge protection on, soft delete 7 d
(SPEC-06 section 3). No secrets are created here; secrets and role assignments come from other tasks (T-403).

- `enable_private_networking = false` (default): public endpoint on, Entra ID + RBAC only. `true`: public access disabled and network ACLs default `Deny` with `AzureServices` bypass; private endpoint is Phase 4.
- `prevent_destroy` is set; purge protection means a deleted vault name stays reserved for the retention period.
- Checkov skips (inline, justified): public endpoint while flag off (CKV_AZURE_189), no private endpoint (CKV2_AZURE_32).

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
| `azurerm_key_vault.this` | resource |
| `azurerm_client_config.current` | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `name` | Vault name, 3-24 chars. | `string` | n/a | yes |
| `resource_group_name` | Resource group for the vault. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `enable_private_networking` | Disable public access, ACL default Deny. | `bool` | `false` | no |
| `soft_delete_retention_days` | Soft delete retention (7-90). | `number` | `7` | no |
| `tags` | Tag map from `module.resource_group.tags` (validated). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags`. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `id` | Key Vault ID. |
| `name` | Key Vault name. |
| `vault_uri` | Key Vault URI. |
<!-- END_TF_DOCS -->
