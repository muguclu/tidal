variable "env" {
  description = "Environment name; forms the resource group name rg-tidal-<env> and the env tag."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
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

variable "owner" {
  description = "Value of the owner tag (person or team accountable for the resources)."
  type        = string

  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "owner must not be empty."
  }
}

variable "cost_center" {
  description = "Value of the cost_center tag."
  type        = string
  default     = "portfolio"

  validation {
    condition     = length(trimspace(var.cost_center)) > 0
    error_message = "cost_center must not be empty."
  }
}

variable "component" {
  description = "Value of the component tag on the resource group itself. Consumers override it per resource."
  type        = string
  default     = "foundation"

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}

variable "park" {
  description = "Value of the park tag: true if the environment is removed or stopped by make park."
  type        = bool
  default     = false
}

variable "extra_tags" {
  description = "Additional tags merged with the mandatory ones. May not redefine project, env, owner, cost_center, component or park."
  type        = map(string)
  default     = {}

  validation {
    condition     = length(setintersection(keys(var.extra_tags), ["project", "env", "owner", "cost_center", "component", "park"])) == 0
    error_message = "extra_tags must not override the mandatory tags (project, env, owner, cost_center, component, park); use the dedicated variables."
  }
}
