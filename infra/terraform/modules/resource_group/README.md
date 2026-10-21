# resource_group

Creates `rg-tidal-<env>` and computes the merged tag map (project, env, owner, cost_center, component, park)
that sibling modules receive through their `tags` input. `extra_tags` may add keys but cannot override the
mandatory ones (validated). The resource group itself costs nothing.

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
| `azurerm_resource_group.this` | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name; forms the resource group name rg-tidal-<env> and the env tag. | `string` | `"dev"` | no |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `owner` | Value of the owner tag (person or team accountable for the resources). | `string` | n/a | yes |
| `cost_center` | Value of the cost_center tag. | `string` | `"portfolio"` | no |
| `component` | Value of the component tag on the resource group itself. | `string` | `"foundation"` | no |
| `park` | Value of the park tag. | `bool` | `false` | no |
| `extra_tags` | Additional tags; may not redefine the mandatory ones. | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| `id` | Resource group resource ID. |
| `name` | Resource group name (rg-tidal-<env>). |
| `location` | Resource group region. |
| `tags` | Merged tag map (mandatory plus extra). |
<!-- END_TF_DOCS -->
