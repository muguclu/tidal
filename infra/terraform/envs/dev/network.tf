# Private networking composition (T-404, SPEC-06 section 4). Everything is gated on var.enable_private_networking (main.tf
# passes the same flag to storage, key_vault and sql, which then close their public endpoints). Flag off: the network
# module creates nothing, its subnet outputs are null and every consumer keeps its current public behaviour.
# Everything network-specific lives in this file, the consumers only read module.network.* (aks.tf, apps.tf, vm.tf).

variable "network_vm_outbound" {
  description = "Outbound path of snet-vm when enable_private_networking is true: public_ip (default, one Standard IP on the VM NIC, about 3.21 EUR/month) or nat_gateway (no public address on the VM, about 32 EUR/month). See docs/finops/deltas/T-404.md."
  type        = string
  default     = "public_ip"

  validation {
    condition     = contains(["public_ip", "nat_gateway"], var.network_vm_outbound)
    error_message = "network_vm_outbound must be public_ip or nat_gateway."
  }
}

module "network" {
  source = "../../modules/network"

  enabled             = var.enable_private_networking
  env                 = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "network"

  vm_outbound = var.network_vm_outbound

  # Blob, SQL and Key Vault (ADR-0006: Service Bus stays public behind RBAC, a private endpoint needs Premium).
  private_endpoint_targets = {
    blob  = module.storage.id
    sql   = module.sql.server_id
    vault = module.key_vault.id
  }

  # The internal Container Apps environment resolves through a private zone named after its default domain.
  container_apps_environment = var.enable_private_networking ? {
    default_domain    = module.container_apps.default_domain
    static_ip_address = module.container_apps.static_ip_address
  } : null
}

output "network_subnet_ids" {
  description = "Subnet IDs by short name (aks, aca, app, pe, vm), or an empty map when enable_private_networking is false."
  value       = module.network.subnet_ids
}

output "network_private_endpoint_ids" {
  description = "Private endpoint IDs (blob, sql, vault), or an empty map when enable_private_networking is false."
  value       = module.network.private_endpoint_ids
}
