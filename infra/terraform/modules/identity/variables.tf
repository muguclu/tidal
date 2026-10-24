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
  description = "Names of the user-assigned managed identities to create (SPEC-06 section 1). Role assignments are passed in through role_assignments (rbac.tf, T-403)."
  type        = set(string)
  default     = ["id-api", "id-query", "id-worker", "id-ingest", "id-tiering", "id-legacy-vm"]

  validation {
    condition     = length(var.identity_names) > 0 && alltrue([for n in var.identity_names : can(regex("^id-[a-z0-9-]{1,60}$", n))])
    error_message = "identity_names must be a non-empty set of names matching ^id-[a-z0-9-]+$ e.g. id-api."
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
  description = "Value of the component tag for the resources in this module (for example observability); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}

variable "role_assignments" {
  description = "Azure role assignments to create, keyed by a stable logical key (identity|role|scope-name). Every entry needs a justification (stored as the assignment description). Data-plane roles (Storage Blob Data *, Azure Service Bus Data *, Key Vault Secrets *) must be scoped to a resource, queue, container or secret, never to a resource group or subscription (SPEC-06 section 2)."
  type = map(object({
    principal_id         = string
    role_definition_name = string
    scope                = string
    justification        = string
    principal_type       = optional(string, "ServicePrincipal")
  }))
  default = {}

  validation {
    condition     = alltrue([for k, v in var.role_assignments : length(trimspace(v.justification)) > 0])
    error_message = "Every role assignment needs a non-empty justification."
  }

  validation {
    condition = alltrue([
      for k, v in var.role_assignments :
      !can(regex("^(Storage Blob Data|Azure Service Bus Data|Key Vault Secrets|Key Vault Crypto)", v.role_definition_name)) || can(regex("/providers/", v.scope))
    ])
    error_message = "Data-plane roles must be scoped to a resource below /providers/ (container, queue or secret), not to a resource group or subscription."
  }

  validation {
    condition     = alltrue([for k, v in var.role_assignments : contains(["ServicePrincipal", "User", "Group"], v.principal_type)])
    error_message = "principal_type must be ServicePrincipal, User or Group."
  }
}

variable "federated_credentials" {
  description = "Federated identity credentials for identities created by this instance, keyed by credential name. identity_name must be in identity_names; subject is the Kubernetes service account subject, for example system:serviceaccount:kube-system:keda-operator."
  type = map(object({
    identity_name = string
    issuer        = string
    subject       = string
  }))
  default = {}

  validation {
    condition     = alltrue([for k, v in var.federated_credentials : can(regex("^system:serviceaccount:[a-z0-9-]+:[a-z0-9-]+$", v.subject))])
    error_message = "subject must look like system:serviceaccount:<namespace>:<service-account>."
  }

  validation {
    condition     = alltrue([for k, v in var.federated_credentials : contains(var.identity_names, v.identity_name)])
    error_message = "identity_name of every federated credential must be one of identity_names."
  }
}
