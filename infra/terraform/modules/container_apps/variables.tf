variable "env" {
  description = "Environment name; part of the resource names."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "tidal_env" {
  description = "Value of the TIDAL_ENV app setting (ADR-0012: explicit, no default in the image). Set on the query app and on every job."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.tidal_env)
    error_message = "tidal_env must be dev or prod (local is for docker compose only)."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the environment, the app and the jobs."
  type        = string

  validation {
    condition     = length(var.resource_group_name) > 0
    error_message = "resource_group_name must not be empty."
  }
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"

  validation {
    condition     = can(regex("^[a-z0-9]+$", var.location))
    error_message = "location must be a lowercase Azure region name without spaces, e.g. westeurope."
  }
}

variable "log_analytics_workspace_id" {
  description = "Log Analytics workspace that receives console and system logs of the environment. null (default) disables log shipping: the shared workspace has a 0.2 GB/day cap (docs/finops/cost-model.md) and the apps log to Application Insights through OpenTelemetry."
  type        = string
  default     = null
}

variable "image" {
  description = "Full image reference for the query app and all jobs (<login_server>/tidal:<tag>). One image, many entrypoints (deploy/docker/Dockerfile). The deploy workflow (T-311) moves the tag, so image changes are ignored after creation."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$", var.image))
    error_message = "image must look like <registry>/<repository>:<tag> or ...@sha256:<digest>."
  }
}

variable "registry_server" {
  description = "ACR login server (for example acrtidaldevabc.azurecr.io). Images are pulled with the identity of each app or job; there is no admin user or password."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+\\.azurecr\\.io$", var.registry_server))
    error_message = "registry_server must be an ACR login server such as acrtidaldevabc.azurecr.io."
  }
}

variable "common_env" {
  description = "Plain (non-secret) environment variables shared by the query app and all jobs, for example TIDAL_STORAGE__ACCOUNT_URL. No secrets: managed identity only (CLAUDE.md principle 3)."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.common_env) : !contains(["TIDAL_ENV", "AZURE_CLIENT_ID", "TIDAL_SERVICE_NAME"], k) && !can(regex("(?i)(PASSWORD|CONNECTION_STRING|SECRET|KEY|TOKEN|SAS|CREDENTIAL)", k)) && !can(regex("(?i)(sig=|AccountKey=|SharedAccessKey=)", var.common_env[k]))])
    error_message = "common_env must not set TIDAL_ENV, AZURE_CLIENT_ID or TIDAL_SERVICE_NAME (the module sets them) and must not carry secrets (names containing PASSWORD, CONNECTION_STRING, SECRET, KEY, TOKEN, SAS or CREDENTIAL, any case) and values must not look like SAS or storage keys (sig=, AccountKey=, SharedAccessKey=)."
  }
}

variable "query" {
  description = "The tidal-query Container App (SPEC-07 section 2): min 0 / max 5 replicas, HTTP scale rule 20 concurrent requests. identity_id is the user-assigned identity (id-query) that pulls the image and reads data; it needs AcrPull on the registry before this module is applied. external_ingress = true because the environment has no VNet yet (T-404) and the App Service API must reach it; every request still needs an Entra JWT (SPEC-06 section 1). The caller IP allow-list is applied outside the module (envs/dev/apps.tf, azapi) because the web app needs this app's FQDN, which would make a module cycle. Set external_ingress false once a VNet exists."
  type = object({
    identity_id         = string
    identity_client_id  = string
    min_replicas        = optional(number, 0)
    max_replicas        = optional(number, 5)
    concurrent_requests = optional(number, 20)
    cpu                 = optional(number, 0.5)
    memory              = optional(string, "1Gi")
    external_ingress    = optional(bool, true)
    extra_env           = optional(map(string), {})
  })

  validation {
    condition     = var.query.min_replicas >= 0 && var.query.max_replicas >= 1 && var.query.max_replicas <= 30 && var.query.min_replicas <= var.query.max_replicas
    error_message = "query replicas: need 0 <= min_replicas <= max_replicas and 1 <= max_replicas <= 30."
  }

  validation {
    condition     = var.query.concurrent_requests >= 1 && var.query.concurrent_requests <= 1000
    error_message = "query.concurrent_requests must be 1 to 1000."
  }

  validation {
    condition     = contains([0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2], var.query.cpu) && can(regex("^[0-9.]+Gi$", var.query.memory))
    error_message = "query.cpu must be a Container Apps consumption size (0.25 to 2 in 0.25 steps) and query.memory like 1Gi."
  }
}

variable "jobs" {
  description = "Container Apps Jobs by short name (resource name caj-tidal-<name>, 20 characters at most). trigger_type schedule needs cron_expression (UTC); event needs queue (Service Bus queue scaled by KEDA with the job identity, no connection string). command and args override the image CMD. Every job runs as its own user-assigned identity and gets TIDAL_ENV, TIDAL_SERVICE_NAME and AZURE_CLIENT_ID."
  type = map(object({
    trigger_type       = string
    cron_expression    = optional(string)
    identity_id        = string
    identity_client_id = string
    service_name       = string
    command            = list(string)
    args               = optional(list(string), [])
    cpu                = optional(number, 0.5)
    memory             = optional(string, "1Gi")
    timeout_seconds    = optional(number, 1800)
    retry_limit        = optional(number, 1)
    max_executions     = optional(number, 1)
    polling_interval   = optional(number, 60)
    queue = optional(object({
      namespace     = string
      queue_name    = string
      message_count = optional(number, 1)
    }))
    extra_env = optional(map(string), {})
  }))
  default = {}

  validation {
    condition     = alltrue([for k, j in var.jobs : can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", k))])
    error_message = "job names must be lowercase letters, digits and hyphens, 2 to 20 characters, starting with a letter."
  }

  validation {
    condition = alltrue([
      for j in values(var.jobs) :
      j.trigger_type == "schedule" ? (j.cron_expression != null && j.queue == null) : (j.trigger_type == "event" && j.queue != null && j.cron_expression == null)
    ])
    error_message = "trigger_type must be schedule (with cron_expression, no queue) or event (with queue, no cron_expression)."
  }

  validation {
    condition     = alltrue([for j in values(var.jobs) : length(j.command) > 0 && j.timeout_seconds >= 60 && j.timeout_seconds <= 86400 && j.retry_limit >= 0 && j.max_executions >= 1])
    error_message = "jobs need a command, timeout_seconds from 60 to 86400, retry_limit >= 0 and max_executions >= 1."
  }

  validation {
    condition     = alltrue([for j in values(var.jobs) : contains([0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2], j.cpu)])
    error_message = "job cpu must be a Container Apps consumption size (0.25 to 2 in 0.25 steps)."
  }
}

variable "tags" {
  description = "Tag map from module.resource_group.tags. Must contain project, env, owner, cost_center, component, park (non-empty), park must be true or false, project must be tidal. The component tag is overridden per resource from var.component."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["project", "env", "owner", "cost_center", "component", "park"] :
      contains(keys(var.tags), k) && length(trimspace(var.tags[k])) > 0
      ]) && (
      !contains(keys(var.tags), "park") ? false : contains(["true", "false"], var.tags["park"])
      ) && (
      !contains(keys(var.tags), "project") ? false : var.tags["project"] == "tidal"
    )
    error_message = "tags must contain non-empty project, env, owner, cost_center, component, park; park must be \"true\" or \"false\"; project must be \"tidal\"."
  }
}

variable "component" {
  description = "Value of the component tag for the resources in this module (for example compute); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
