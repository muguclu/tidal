output "enabled" {
  description = "True when the network exists (flag on)."
  value       = local.on
}

output "vnet_id" {
  description = "VNet resource ID, or null when disabled."
  value       = one(azurerm_virtual_network.this[*].id)
}

output "subnet_ids" {
  description = "Subnet IDs by short name (aks, aca, app, pe, vm), or an empty map when disabled."
  value = local.on ? {
    aks = azurerm_subnet.aks[0].id
    aca = azurerm_subnet.aca[0].id
    app = azurerm_subnet.app[0].id
    pe  = azurerm_subnet.pe[0].id
    vm  = azurerm_subnet.vm[0].id
  } : {}
}

output "aks_subnet_id" {
  description = "snet-aks ID for the AKS node pools (vnet_subnet_id), or null when disabled."
  value       = one(azurerm_subnet.aks[*].id)
}

output "aca_subnet_id" {
  description = "snet-aca ID for the Container Apps environment infrastructure subnet, or null when disabled."
  value       = one(azurerm_subnet.aca[*].id)
}

output "app_subnet_id" {
  description = "snet-app ID for App Service regional VNet integration, or null when disabled."
  value       = one(azurerm_subnet.app[*].id)
}

output "vm_subnet_id" {
  description = "snet-vm ID for the legacy VM NIC, or null when disabled."
  value       = one(azurerm_subnet.vm[*].id)
}

output "pe_subnet_cidr" {
  description = "Address prefix of snet-pe (known at plan time), or null when disabled. Used to scope the VM NIC NSG outbound rule."
  value       = local.on ? local.subnets.pe : null
}

output "vm_public_ip_id" {
  description = "Public IP ID to attach to the VM NIC when vm_outbound is public_ip; null when disabled or when the NAT gateway owns the address."
  value       = local.on && var.vm_outbound == "public_ip" ? azurerm_public_ip.vm_outbound[0].id : null
}

output "private_endpoint_ids" {
  description = "Private endpoint IDs by service (blob, sql, vault); empty when disabled."
  value       = { for k, v in azurerm_private_endpoint.this : k => v.id }
}

output "private_dns_zone_ids" {
  description = "Private DNS zone IDs by service (blob, sql, vault); empty when disabled."
  value       = { for k, v in azurerm_private_dns_zone.pe : k => v.id }
}
