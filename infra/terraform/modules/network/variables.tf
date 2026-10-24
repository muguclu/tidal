variable "enabled" {
  description = "Master switch, wired to enable_private_networking in envs/dev. false (default) creates nothing and every output is null or empty, so the module costs nothing."
  type        = bool
  default     = false
}

variable "env" {
  description = "Environment name used in resource names (for example dev)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{2,8}$", var.env))
    error_message = "env must be 2-8 lowercase letters or digits."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the VNet, private endpoints, DNS zones and NAT gateway / public IP."
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

variable "address_space" {
  description = "VNet address space, a /16. It must not overlap the AKS pod CIDR (10.244.0.0/16) or service CIDR (10.0.0.0/16). The five subnets are carved from it with cidrsubnet()."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.address_space, 0)) && endswith(var.address_space, "/16")
    error_message = "address_space must be a valid /16 CIDR block, e.g. 10.40.0.0/16."
  }

  validation {
    condition     = !startswith(var.address_space, "10.244.") && !startswith(var.address_space, "10.0.")
    error_message = "address_space must not overlap the AKS pod CIDR 10.244.0.0/16 or service CIDR 10.0.0.0/16."
  }
}

variable "vm_outbound" {
  description = "How snet-vm reaches the internet once default outbound access is off (ARM deallocate, Entra, PyPI and apt need HTTPS out). public_ip (default): one Standard static public IP that the caller attaches to the VM NIC, about 3.21 EUR/month, outbound only because the NIC NSG denies all inbound. nat_gateway: Standard NAT gateway that owns the IP, no public address on the VM, about 32 EUR/month (docs/finops/deltas/T-404.md). Both bill 24/7, also while the VM is deallocated."
  type        = string
  default     = "public_ip"

  validation {
    condition     = contains(["public_ip", "nat_gateway"], var.vm_outbound)
    error_message = "vm_outbound must be public_ip or nat_gateway."
  }
}

variable "private_endpoint_targets" {
  description = "Resource IDs to publish through private endpoints in snet-pe, keyed by service: blob (storage account ID), sql (SQL server ID), vault (Key Vault ID). Each entry creates one private endpoint, one private DNS zone and one VNet link. The keys must be known at plan time, the IDs may be computed. Service Bus has no entry on purpose: private endpoints need Premium (ADR-0006)."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.private_endpoint_targets) : contains(["blob", "sql", "vault"], k)])
    error_message = "private_endpoint_targets keys must be blob, sql or vault."
  }
}

variable "container_apps_environment" {
  description = "Private DNS for an internal Container Apps environment: default_domain (for example nice-sea-123.westeurope.azurecontainerapps.io) and static_ip_address of the environment, from the container_apps module. Creates a zone named default_domain with wildcard and apex A records and links it to the VNet. null (default) creates no zone."
  type = object({
    default_domain    = string
    static_ip_address = string
  })
  default = null
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
  description = "Value of the component tag for the resources in this module."
  type        = string
  default     = "network"

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
