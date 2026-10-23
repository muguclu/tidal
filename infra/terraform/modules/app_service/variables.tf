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
  description = "Value of the TIDAL_ENV app setting (ADR-0012: explicit, no default in the image)."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.tidal_env)
    error_message = "tidal_env must be dev or prod (local is for docker compose only)."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the plan and the web app."
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
  description = "Suffix that makes the web app host name (<name>.azurewebsites.net) globally unique. 3 to 8 lowercase letters or digits."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.name_suffix))
    error_message = "name_suffix must be 3 to 8 lowercase letters or digits."
  }
}

variable "sku_name" {
  description = "App Service plan SKU. B1 is the SPEC-07 section 2 choice (smallest tier with Always On and custom containers). Any other value needs a justification and a cost-model.md update."
  type        = string
  default     = "B1"

  validation {
    condition     = contains(["B1", "B2", "B3"], var.sku_name)
    error_message = "sku_name must be B1, B2 or B3 (Basic Linux tiers)."
  }
}

variable "registry_server" {
  description = "ACR login server (for example acrtidaldevabc.azurecr.io); used with the managed identity pull, no admin credentials."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+\\.azurecr\\.io$", var.registry_server))
    error_message = "registry_server must be an ACR login server such as acrtidaldevabc.azurecr.io."
  }
}

variable "image_name" {
  description = "Repository and tag inside the registry (for example tidal:latest). The deploy workflow (T-311) moves the tag, so changes are ignored after creation."
  type        = string
  default     = "tidal:latest"

  validation {
    condition     = can(regex("^[a-z0-9._/-]+:[A-Za-z0-9._-]+$", var.image_name))
    error_message = "image_name must look like <repository>:<tag>."
  }
}

variable "identity_id" {
  description = "Resource ID of the user-assigned identity (id-api) of the app. It pulls the image (needs AcrPull on the registry before this module is applied) and carries the data roles of SPEC-06 section 2."
  type        = string
}

variable "identity_client_id" {
  description = "Client ID of the same identity; set as AZURE_CLIENT_ID and as the registry pull identity."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.identity_client_id))
    error_message = "identity_client_id must be a GUID."
  }
}

variable "health_check_path" {
  description = "HTTP path App Service probes; the API serves /v1/health/live (deploy/docker/Dockerfile notes)."
  type        = string
  default     = "/v1/health/live"

  validation {
    condition     = startswith(var.health_check_path, "/")
    error_message = "health_check_path must start with /."
  }
}

variable "container_port" {
  description = "Port the tidal-api container listens on (WEBSITES_PORT)."
  type        = number
  default     = 8000

  validation {
    condition     = var.container_port >= 1 && var.container_port <= 65535
    error_message = "container_port must be a valid TCP port."
  }
}

variable "app_settings" {
  description = "Plain (non-secret) app settings, for example TIDAL_STORAGE__ACCOUNT_URL and TIDAL_QUERY__BASE_URL. Secrets are not allowed: use Key Vault references (SPEC-06 section 3) or, better, managed identity."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.app_settings) : !contains(["TIDAL_ENV", "AZURE_CLIENT_ID", "TIDAL_SERVICE_NAME", "WEBSITES_PORT"], k) && !can(regex("(PASSWORD|CONNECTION_STRING|SECRET|KEY)$", k))])
    error_message = "app_settings must not set TIDAL_ENV, AZURE_CLIENT_ID, TIDAL_SERVICE_NAME or WEBSITES_PORT (the module sets them) and must not carry secrets (names ending in PASSWORD, CONNECTION_STRING, SECRET or KEY)."
  }
}

variable "vnet_integration_subnet_id" {
  description = "Subnet ID (snet-app) for regional VNet integration (SPEC-07 section 2 flag). null until the network module exists (T-404)."
  type        = string
  default     = null
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
