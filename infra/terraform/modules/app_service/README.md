# app_service

Linux App Service plan (**B1**, SPEC-07 section 2) and the `tidal-api` web app, as a container from ACR.

- **Image pull with managed identity:** `container_registry_use_managed_identity` with the client ID of `id-api`; no admin user, no password. `id-api` needs `AcrPull` on the registry *before* apply (granted in `envs/dev/apps.tf`, scoped to the registry).
- **Health:** `health_check_path` = `/v1/health/live` (the image has no HEALTHCHECK, deploy/docker/Dockerfile), Always On, auto-heal recycles on repeated 5xx.
- **Settings:** `TIDAL_ENV` (explicit, ADR-0012), `TIDAL_SERVICE_NAME=api`, `AZURE_CLIENT_ID`, `WEBSITES_PORT`. Extra plain settings go through `app_settings`; names that look like secrets (`PASSWORD`, `CONNECTION_STRING`, `SECRET`, `KEY`, `TOKEN`, `SAS`, `CREDENTIAL`) are rejected by validation unless the value is a Key Vault reference `@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<name>)` (resolved with the module identity via `key_vault_reference_identity_id`, so no secret value enters state). `TIDAL_CURSOR__KEYVAULT_URL` is the one exempt name and must be a vault URL; values containing `sig=`, `AccountKey=` or `SharedAccessKey=` are always rejected.
- **Security:** HTTPS only, TLS 1.2, FTPS and basic-auth publishing off, no remote debugging.
- **Flag:** the env composition wraps the module in `count = var.enable_app_service ? 1 : 0` so the always-on B1 plan can be removed while parked.
- **VNet integration** (`vnet_integration_subnet_id`) stays null until the network module (T-404).
- Cost: see `docs/finops/deltas/T-309.md`.

## Checkov skips

Each skip carries its justification inline in `main.tf` (B1 is not a production SKU, single instance, no zone redundancy, in-app Entra JWT validation instead of built-in auth, public network until T-404, logs via OpenTelemetry).

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
| <a name="requirement_azurerm"></a> [azurerm](#requirement\_azurerm) | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| [azurerm_linux_web_app.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/linux_web_app) | resource |
| [azurerm_service_plan.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/service_plan) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_app_settings"></a> [app\_settings](#input\_app\_settings) | Plain (non-secret) app settings, for example TIDAL\_STORAGE\_\_ACCOUNT\_URL and TIDAL\_QUERY\_\_BASE\_URL. Secrets are not allowed: use Key Vault references (SPEC-06 section 3) or, better, managed identity. | `map(string)` | `{}` | no |
| <a name="input_component"></a> [component](#input\_component) | Value of the component tag for the resources in this module (for example compute); merged over tags["component"] inside the module. | `string` | n/a | yes |
| <a name="input_container_port"></a> [container\_port](#input\_container\_port) | Port the tidal-api container listens on (WEBSITES\_PORT). | `number` | `8000` | no |
| <a name="input_env"></a> [env](#input\_env) | Environment name; part of the resource names. | `string` | `"dev"` | no |
| <a name="input_health_check_path"></a> [health\_check\_path](#input\_health\_check\_path) | HTTP path App Service probes; the API serves /v1/health/live (deploy/docker/Dockerfile notes). | `string` | `"/v1/health/live"` | no |
| <a name="input_identity_client_id"></a> [identity\_client\_id](#input\_identity\_client\_id) | Client ID of the same identity; set as AZURE\_CLIENT\_ID and as the registry pull identity. | `string` | n/a | yes |
| <a name="input_identity_id"></a> [identity\_id](#input\_identity\_id) | Resource ID of the user-assigned identity (id-api) of the app. It pulls the image (needs AcrPull on the registry before this module is applied) and carries the data roles of SPEC-06 section 2. | `string` | n/a | yes |
| <a name="input_image_name"></a> [image\_name](#input\_image\_name) | Repository and tag inside the registry (for example tidal:v0.1.0). The tag must not be latest. The deploy workflow (T-311) moves the tag, so changes are ignored after creation. | `string` | n/a | yes |
| <a name="input_location"></a> [location](#input\_location) | Azure region. | `string` | `"westeurope"` | no |
| <a name="input_log_retention_days"></a> [log\_retention\_days](#input\_log\_retention\_days) | Days of application and HTTP logs kept on the app filesystem (cheapest option: no Log Analytics ingestion, which is capped at 0.2 GB/day). 1 to 7. | `number` | `3` | no |
| <a name="input_name_suffix"></a> [name\_suffix](#input\_name\_suffix) | Suffix that makes the web app host name (<name>.azurewebsites.net) globally unique. 3 to 8 lowercase letters or digits. | `string` | n/a | yes |
| <a name="input_registry_server"></a> [registry\_server](#input\_registry\_server) | ACR login server (for example acrtidaldevabc.azurecr.io); used with the managed identity pull, no admin credentials. | `string` | n/a | yes |
| <a name="input_resource_group_name"></a> [resource\_group\_name](#input\_resource\_group\_name) | Resource group that holds the plan and the web app. | `string` | n/a | yes |
| <a name="input_sku_name"></a> [sku\_name](#input\_sku\_name) | App Service plan SKU. B1 is the SPEC-07 section 2 choice (smallest tier with Always On and custom containers). Any other value needs a justification and a cost-model.md update. | `string` | `"B1"` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tag map from module.resource\_group.tags. Must contain project, env, owner, cost\_center, component, park (non-empty), park must be true or false, project must be tidal. The component tag is overridden per resource from var.component. | `map(string)` | n/a | yes |
| <a name="input_tidal_env"></a> [tidal\_env](#input\_tidal\_env) | Value of the TIDAL\_ENV app setting (ADR-0012: explicit, no default in the image). | `string` | n/a | yes |
| <a name="input_vnet_integration_subnet_id"></a> [vnet\_integration\_subnet\_id](#input\_vnet\_integration\_subnet\_id) | Subnet ID (snet-app) for regional VNet integration (SPEC-07 section 2 flag). null until the network module exists (T-404). | `string` | `null` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_default_hostname"></a> [default\_hostname](#output\_default\_hostname) | Default host name (<name>.azurewebsites.net); managed TLS applies. |
| <a name="output_id"></a> [id](#output\_id) | Resource ID of the web app. |
| <a name="output_name"></a> [name](#output\_name) | Web app name; target of the deploy workflow (T-311) and of az webapp stop in the park script (T-313). |
| <a name="output_plan_id"></a> [plan\_id](#output\_plan\_id) | Resource ID of the App Service plan (the B1 plan is the billed resource). |
| <a name="output_possible_outbound_ip_addresses"></a> [possible\_outbound\_ip\_addresses](#output\_possible\_outbound\_ip\_addresses) | Every outbound IP the web app can use (possible\_outbound\_ip\_address\_list); the allow-list for tidal-query ingress. |

<!-- END_TF_DOCS -->
