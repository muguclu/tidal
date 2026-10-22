variable "env" {
  description = "Environment name; used in the action group name (ag-tidal-<env>)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the action group (the action group itself is a global resource)."
  type        = string

  validation {
    condition     = length(var.resource_group_name) > 0
    error_message = "resource_group_name must not be empty."
  }
}

variable "resource_group_id" {
  description = "Resource ID of the resource group; the budget scope when budget_scope is resource_group. The subscription ID is derived from it when budget_scope is subscription."
  type        = string

  validation {
    condition     = can(regex("(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+$", var.resource_group_id))
    error_message = "resource_group_id must be a resource group ID: /subscriptions/<guid>/resourceGroups/<name>."
  }
}

variable "alert_emails" {
  description = "Email addresses that receive budget and (later) platform alerts through the action group and as budget contact emails. Supplied by the env composition; never hard-coded in the module."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0 && length(var.alert_emails) <= 10
    error_message = "alert_emails must contain 1 to 10 addresses."
  }

  validation {
    condition     = alltrue([for e in var.alert_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", e))])
    error_message = "Every alert_emails entry must be a valid email address."
  }

  validation {
    condition     = length(distinct([for e in var.alert_emails : lower(e)])) == length(var.alert_emails)
    error_message = "alert_emails must not contain duplicates."
  }
}

variable "action_group_short_name" {
  description = "Action group short name shown in SMS and email notifications (maximum 12 characters)."
  type        = string
  default     = "tidal"

  validation {
    condition     = can(regex("^[A-Za-z0-9-]{1,12}$", var.action_group_short_name))
    error_message = "action_group_short_name must be 1 to 12 letters, digits or hyphens."
  }
}

variable "budget_scope" {
  description = "Budget scope. resource_group (default) needs only the Contributor the CI identity holds on rg-tidal-<env> (SPEC-06 section 2) but ignores spend outside the group. subscription covers all spend (needed for the cloud-window credit, LOCAL-DEV section 2) and needs subscription-level rights, so a human applies it."
  type        = string
  default     = "resource_group"

  validation {
    condition     = contains(["resource_group", "subscription"], var.budget_scope)
    error_message = "budget_scope must be resource_group or subscription."
  }
}

variable "amount" {
  description = "Monthly budget amount in the subscription billing currency (SPEC-07 section 2: 60). Azure budgets have no currency argument and are always evaluated in the billing currency (EUR or USD), so the number must match the account currency."
  type        = number
  default     = 60

  validation {
    condition     = var.amount > 0 && var.amount <= 100000
    error_message = "amount must be greater than 0 and at most 100000."
  }
}

variable "start_date" {
  description = "First day of the budget period, RFC 3339 at midnight UTC on the first of a month, for example 2026-11-01T00:00:00Z. Azure rejects start dates earlier than the first of the current month for new budgets, so set it at creation time."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-(0[1-9]|1[0-2])-01T00:00:00Z$", var.start_date))
    error_message = "start_date must be the first day of a month at midnight UTC, e.g. 2026-11-01T00:00:00Z."
  }
}

variable "end_date" {
  description = "Optional last day of the budget period (RFC 3339). null lets Azure default to ten years after start_date."
  type        = string
  default     = null

  validation {
    condition     = var.end_date == null || can(formatdate("YYYY", var.end_date))
    error_message = "end_date must be null or an RFC 3339 timestamp."
  }
}

variable "enable_anomaly_alert" {
  description = "Create the subscription-scoped azurerm_cost_anomaly_alert (PLANNING section 6). It needs subscription-level rights (Cost Management Contributor) that the CI identity does not have, so keep false in CI and let a human apply it."
  type        = bool
  default     = false
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
  description = "Value of the component tag for the resources in this module (for example finops); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
