# identity

User-assigned managed identities for the Tidal workloads (SPEC-06 section 1). Default set:
`id-api`, `id-query`, `id-worker`, `id-ingest`, `id-tiering`, `id-legacy-vm`.

This module creates identities only. All role assignments are owned by T-403 so RBAC is reviewed in one
place, scoped per SPEC-06 section 2. AKS federated credentials are added by the AKS task. Identities are free.

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
| `azurerm_user_assigned_identity.this` | resource (for_each over `identity_names`) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `resource_group_name` | Resource group that holds the identities. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `identity_names` | Identity names to create; must match `^id-[a-z0-9-]+$`. | `set(string)` | six SPEC-06 names | no |
| `tags` | Complete tag map with the six mandatory keys; `component` is set per identity. | `map(string)` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `identities` | Map of name to `{ id, principal_id, client_id }`. |
<!-- END_TF_DOCS -->
