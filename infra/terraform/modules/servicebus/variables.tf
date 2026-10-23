variable "env" {
  description = "Environment name; used in the namespace name (sb-tidal-<env>-<suffix>)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the Service Bus namespace."
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

variable "name_suffix" {
  description = "Suffix that makes the namespace name globally unique (sb-tidal-<env>-<suffix>); supplied by the env composition. The full name is 6 to 50 characters by construction (3 to 8 character suffix)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.name_suffix))
    error_message = "name_suffix must be 3 to 8 lowercase letters or digits."
  }
}

variable "enable_ingest_topic" {
  description = "Create the optional ingest-events topic with its stats and quality-alerts subscriptions (messaging-contract section 1, ADR-0003). Topics need the Standard SKU, so true switches the namespace from Basic to Standard (adds a monthly base charge, see docs/finops/deltas/T-124.md)."
  type        = bool
  default     = false
}

variable "queues" {
  description = "Queues keyed by name. Defaults equal messaging-contract section 1 exactly. null lock_duration / default_message_ttl / max_delivery_count means the Service Bus default (PT1M / the tier maximum TTL, 14 days on Basic / 10). Basic has no sessions or duplicate detection, so neither is configurable."
  type = map(object({
    lock_duration                        = optional(string)
    max_delivery_count                   = optional(number)
    default_message_ttl                  = optional(string)
    dead_lettering_on_message_expiration = optional(bool, false)
  }))

  default = {
    "backtest-jobs" = {
      lock_duration                        = "PT5M"
      max_delivery_count                   = 3
      default_message_ttl                  = "P1D"
      dead_lettering_on_message_expiration = true
    }
    "backtest-results" = {
      max_delivery_count                   = 5
      dead_lettering_on_message_expiration = true
    }
    "tiering-jobs" = {
      max_delivery_count                   = 3
      default_message_ttl                  = "P1D"
      dead_lettering_on_message_expiration = true
    }
  }

  validation {
    condition     = alltrue([for k in keys(var.queues) : can(regex("^[A-Za-z0-9]([A-Za-z0-9._-]{0,258}[A-Za-z0-9])?$", k))])
    error_message = "Queue names must be 1 to 260 characters of letters, digits, '.', '-' or '_', starting and ending alphanumeric."
  }

  validation {
    condition     = alltrue([for q in values(var.queues) : q.lock_duration == null ? true : can(regex("^PT([1-5]M|[1-9]?[0-9]S|[1-4]M[0-5]?[0-9]S)$", q.lock_duration))])
    error_message = "lock_duration must be an ISO 8601 duration of at most 5 minutes (Service Bus maximum), e.g. PT30S or PT5M."
  }

  validation {
    condition     = alltrue([for q in values(var.queues) : q.max_delivery_count == null ? true : q.max_delivery_count >= 1])
    error_message = "max_delivery_count must be at least 1."
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
  description = "Value of the component tag for the resources in this module (for example messaging); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
