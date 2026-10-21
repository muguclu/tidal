variable "resource_group_name" {
  description = "Resource group that holds the identities."
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

variable "identity_names" {
  description = "Names of the user-assigned managed identities to create (SPEC-06 section 1). Role assignments are NOT created here (T-403)."
  type        = set(string)
  default     = ["id-api", "id-query", "id-worker", "id-ingest", "id-tiering", "id-legacy-vm"]

  validation {
    condition     = length(var.identity_names) > 0 && alltrue([for n in var.identity_names : can(regex("^id-[a-z0-9-]{1,60}$", n))])
    error_message = "identity_names must be a non-empty set of names matching ^id-[a-z0-9-]+$ (for example id-api)."
  }
}

variable "tags" {
  description = "Complete tag map for this resource. Must contain the mandatory keys project, env, owner, cost_center, component, park (pass module.resource_group.tags). The component tag is set to the identity name per resource."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["project", "env", "owner", "cost_center", "component", "park"] : contains(keys(var.tags), k) && length(trimspace(var.tags[k])) > 0])
    error_message = "tags must contain non-empty values for: project, env, owner, cost_center, component, park."
  }
}
