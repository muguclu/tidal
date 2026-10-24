# identity

User-assigned managed identities for the Tidal workloads (SPEC-06 section 1). Default set:
`id-api`, `id-query`, `id-worker`, `id-ingest`, `id-tiering`, `id-legacy-vm`.

This module creates identities and, through `rbac.tf` (T-403), the role assignments and federated credentials passed in
as data. The dev environment declares every SPEC-06 section 2 row in `envs/dev/rbac.tf`, so RBAC is reviewed in one place.
Data-plane roles must be scoped below `/providers/` (validated). AKS workload identity credentials of the workload
identities are created by the AKS module. Identities and assignments are free.

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
| `azurerm_role_assignment.this` | resource (for_each over `role_assignments`) |
| `azurerm_federated_identity_credential.this` | resource (for_each over `federated_credentials`) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `resource_group_name` | Resource group that holds the identities. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `identity_names` | Identity names to create; must match `^id-[a-z0-9-]+$`. | `set(string)` | six SPEC-06 names | no |
| `tags` | Tag map from `module.resource_group.tags` (validated: six keys, park true/false, project tidal). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags` in the module. | `string` | n/a | yes |
| `role_assignments` | Map of key to `{ principal_id, role_definition_name, scope, justification, principal_type }`; data-plane roles must be resource scoped. | `map(object)` | `{}` | no |
| `federated_credentials` | Map of name to `{ identity_name, issuer, subject }` for identities of this instance. | `map(object)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| `identities` | Map of name to `{ id, principal_id, client_id }`. |
| `role_assignment_ids` | Map of key to role assignment ID. |
| `federated_credential_ids` | Map of credential name to ID. |
<!-- END_TF_DOCS -->
