variable "tags" {
  description = "Tag map from module.resource_group.tags. Must contain project, env, owner, cost_center, component, park (non-empty), park must be true or false, project must be tidal. Entra applications only carry a set of strings, so each pair is rendered as \"key:value\"."
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
  description = "Value of the component tag for the resources in this module (for example identity); merged over tags[\"component\"]."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}

variable "api_identifier_uri" {
  description = "Identifier URI of the tidal-api registration. SPEC-06 section 1 fixes api://tidal; it is TIDAL_AUTH__AUDIENCE."
  type        = string
  default     = "api://tidal"

  validation {
    condition     = can(regex("^api://[a-z0-9][a-z0-9._-]*$", var.api_identifier_uri))
    error_message = "api_identifier_uri must look like api://<lower-case-name>."
  }
}

variable "query_identifier_uri" {
  description = "Identifier URI of the tidal-query registration; it is TIDAL_QUERY__AUDIENCE (config default api://tidal-query)."
  type        = string
  default     = "api://tidal-query"

  validation {
    condition     = can(regex("^api://[a-z0-9][a-z0-9._-]*$", var.query_identifier_uri)) && var.query_identifier_uri != var.api_identifier_uri
    error_message = "query_identifier_uri must look like api://<lower-case-name> and differ from api_identifier_uri."
  }
}

variable "role_group_object_ids" {
  description = "Entra group object IDs assigned to each tidal-api app role, keyed by role value (Tidal.Reader, Tidal.Trader, Tidal.Quant, Tidal.Admin). Roles go to groups, not users (SPEC-06 section 1). Missing keys or empty sets assign nobody. Group IDs must be known at plan time."
  type        = map(set(string))
  default     = {}

  validation {
    condition     = alltrue([for r in keys(var.role_group_object_ids) : contains(["Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin"], r)])
    error_message = "role_group_object_ids keys must be among Tidal.Reader, Tidal.Trader, Tidal.Quant, Tidal.Admin."
  }

  validation {
    condition     = alltrue(flatten([for ids in values(var.role_group_object_ids) : [for id in ids : can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", id))]]))
    error_message = "Every group object ID must be a GUID."
  }
}

variable "query_read_principal_object_ids" {
  description = "Service principal object IDs (managed identity principal_id) granted the Tidal.Query.Read app role on tidal-query, keyed by a static label such as id-api. SPEC-06 and T-402: only id-api by default; add id-worker only when ADR-0016 worker reads go live. Keys must be known at plan time, values may be computed."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.query_read_principal_object_ids) : can(regex("^id-[a-z-]+$", k))])
    error_message = "Keys must be managed identity names such as id-api."
  }
}

variable "owner_object_ids" {
  description = "Object IDs that own the three applications and service principals (users, groups or service principals). The azuread provider does not add the caller itself; leave empty only if a tenant admin manages them."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.owner_object_ids : can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", id))])
    error_message = "owner_object_ids must contain GUIDs."
  }
}
