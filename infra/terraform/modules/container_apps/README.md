# container_apps

Container Apps **consumption** environment `cae-tidal-<env>` (SPEC-07 section 2, ADR-0002) with:

- `ca-tidal-query`: min **0** / max 5 replicas, HTTP scale rule at 20 concurrent requests, port 8000, liveness and startup probes on `/health/live`, command `tidal-query`. External ingress by default (no VNet before T-404; the App Service API must reach it); every request still needs an Entra JWT. Set `query.external_ingress = false` once a VNet exists.
- Jobs (`jobs` map, resource name `caj-tidal-<name>`): `schedule` (cron, UTC) or `event` (Service Bus queue, KEDA `azure-servicebus` scaler). The event scaler (needs Data Owner on the queue to read counts; `enable_tiering_event_job` in envs/dev stays false until T-403) authenticates with the job identity (`identity_id` on the rule), so there is no connection string or secret. That identity needs *Azure Service Bus Data Receiver* on the queue (T-403, SPEC-06 section 2).
- Every app and job uses a **user-assigned identity** from the identity module for the image pull and for data access, and gets `TIDAL_ENV` (ADR-0012), `TIDAL_SERVICE_NAME` and `AZURE_CLIENT_ID`. Callers cannot override those keys. Each identity needs `AcrPull` on the registry before apply (granted in `envs/dev/apps.tf`).
- Image changes are ignored after creation (the deploy workflow, T-311, rolls tags).
- Console and system logs go to `log_analytics_workspace_id` when set (envs/dev wires the shared workspace, 0.2 GB/day cap). `TIDAL_OTEL__CONNECTION_STRING` is not wired: it needs a Key Vault secret that no module creates yet.
- Ingress IP allow-list (web app outbound IPs) is patched in `envs/dev/apps.tf` with azapi to avoid a module cycle; the module ignores changes to `ingress[0].ip_security_restriction`.
- Cost: see `docs/finops/deltas/T-309.md`. Container Apps compute is MEASURED, not modelled (decision C5).

Checkov evaluates 0 resources here: it has no policies for the container app resource types.

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
| [azurerm_container_app.query](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app) | resource |
| [azurerm_container_app_environment.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_environment) | resource |
| [azurerm_container_app_job.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_job) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_common_env"></a> [common\_env](#input\_common\_env) | Plain (non-secret) environment variables shared by the query app and all jobs, for example TIDAL\_STORAGE\_\_ACCOUNT\_URL. No secrets: managed identity only (CLAUDE.md principle 3). | `map(string)` | `{}` | no |
| <a name="input_component"></a> [component](#input\_component) | Value of the component tag for the resources in this module (for example compute); merged over tags["component"] inside the module. | `string` | n/a | yes |
| <a name="input_env"></a> [env](#input\_env) | Environment name; part of the resource names. | `string` | `"dev"` | no |
| <a name="input_image"></a> [image](#input\_image) | Full image reference for the query app and all jobs (<login\_server>/tidal:<tag>). One image, many entrypoints (deploy/docker/Dockerfile). The deploy workflow (T-311) moves the tag, so image changes are ignored after creation. | `string` | n/a | yes |
| <a name="input_jobs"></a> [jobs](#input\_jobs) | Container Apps Jobs by short name (resource name caj-tidal-<name>, 20 characters at most). trigger\_type schedule needs cron\_expression (UTC); event needs queue (Service Bus queue scaled by KEDA with the job identity, no connection string). command and args override the image CMD. Every job runs as its own user-assigned identity and gets TIDAL\_ENV, TIDAL\_SERVICE\_NAME and AZURE\_CLIENT\_ID. | <pre>map(object({<br/>    trigger_type       = string<br/>    cron_expression    = optional(string)<br/>    identity_id        = string<br/>    identity_client_id = string<br/>    service_name       = string<br/>    command            = list(string)<br/>    args               = optional(list(string), [])<br/>    cpu                = optional(number, 0.5)<br/>    memory             = optional(string, "1Gi")<br/>    timeout_seconds    = optional(number, 1800)<br/>    retry_limit        = optional(number, 1)<br/>    max_executions     = optional(number, 1)<br/>    polling_interval   = optional(number, 60)<br/>    queue = optional(object({<br/>      namespace     = string<br/>      queue_name    = string<br/>      message_count = optional(number, 1)<br/>    }))<br/>    extra_env = optional(map(string), {})<br/>  }))</pre> | `{}` | no |
| <a name="input_location"></a> [location](#input\_location) | Azure region. | `string` | `"westeurope"` | no |
| <a name="input_log_analytics_workspace_id"></a> [log\_analytics\_workspace\_id](#input\_log\_analytics\_workspace\_id) | Log Analytics workspace that receives console and system logs of the environment. null (default) disables log shipping: the shared workspace has a 0.2 GB/day cap (docs/finops/cost-model.md) and the apps log to Application Insights through OpenTelemetry. | `string` | `null` | no |
| <a name="input_query"></a> [query](#input\_query) | The tidal-query Container App (SPEC-07 section 2): min 0 / max 5 replicas, HTTP scale rule 20 concurrent requests. identity\_id is the user-assigned identity (id-query) that pulls the image and reads data; it needs AcrPull on the registry before this module is applied. external\_ingress = true because the environment has no VNet yet (T-404) and the App Service API must reach it; every request still needs an Entra JWT (SPEC-06 section 1). The caller IP allow-list is applied outside the module (envs/dev/apps.tf, azapi) because the web app needs this app's FQDN, which would make a module cycle. Set external\_ingress false once a VNet exists. | <pre>object({<br/>    identity_id         = string<br/>    identity_client_id  = string<br/>    min_replicas        = optional(number, 0)<br/>    max_replicas        = optional(number, 5)<br/>    concurrent_requests = optional(number, 20)<br/>    cpu                 = optional(number, 0.5)<br/>    memory              = optional(string, "1Gi")<br/>    external_ingress    = optional(bool, true)<br/>    extra_env           = optional(map(string), {})<br/>  })</pre> | n/a | yes |
| <a name="input_registry_server"></a> [registry\_server](#input\_registry\_server) | ACR login server (for example acrtidaldevabc.azurecr.io). Images are pulled with the identity of each app or job; there is no admin user or password. | `string` | n/a | yes |
| <a name="input_resource_group_name"></a> [resource\_group\_name](#input\_resource\_group\_name) | Resource group that holds the environment, the app and the jobs. | `string` | n/a | yes |
| <a name="input_tags"></a> [tags](#input\_tags) | Tag map from module.resource\_group.tags. Must contain project, env, owner, cost\_center, component, park (non-empty), park must be true or false, project must be tidal. The component tag is overridden per resource from var.component. | `map(string)` | n/a | yes |
| <a name="input_tidal_env"></a> [tidal\_env](#input\_tidal\_env) | Value of the TIDAL\_ENV app setting (ADR-0012: explicit, no default in the image). Set on the query app and on every job. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_environment_id"></a> [environment\_id](#output\_environment\_id) | Resource ID of the Container Apps environment. |
| <a name="output_job_ids"></a> [job\_ids](#output\_job\_ids) | Map of job short name to resource ID. |
| <a name="output_job_names"></a> [job\_names](#output\_job\_names) | Map of job short name to the Container Apps Job resource name. |
| <a name="output_query_app_id"></a> [query\_app\_id](#output\_query\_app\_id) | Resource ID of the tidal-query Container App (target of the ingress IP allow-list patch in envs/dev/apps.tf). |
| <a name="output_query_app_name"></a> [query\_app\_name](#output\_query\_app\_name) | Name of the tidal-query Container App (park script and deploy workflow target). |
| <a name="output_query_fqdn"></a> [query\_fqdn](#output\_query\_fqdn) | Ingress FQDN of tidal-query; maps to TIDAL\_QUERY\_\_BASE\_URL as https://<fqdn>. null when the app has no ingress. |

<!-- END_TF_DOCS -->
