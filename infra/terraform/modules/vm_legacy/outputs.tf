output "vm_id" {
  description = "Resource ID of the legacy VM; scope of the park script (az vm deallocate) and of its two custom roles."
  value       = azurerm_linux_virtual_machine.this.id
}

output "vm_name" {
  description = "Name of the legacy VM (vm-tidal-legacy-<env>)."
  value       = azurerm_linux_virtual_machine.this.name
}

output "private_ip_address" {
  description = "Private IP of the VM NIC. There is no public IP."
  value       = azurerm_network_interface.this.private_ip_address
}

output "logic_app_id" {
  description = "Resource ID of the Logic App (Consumption) that starts the VM daily."
  value       = azurerm_logic_app_workflow.start.id
}

output "logic_app_name" {
  description = "Name of the Logic App; the park script disables it with az logic workflow update --state Disabled (or the start_enabled variable)."
  value       = azurerm_logic_app_workflow.start.name
}

output "logic_app_principal_id" {
  description = "Principal ID of the Logic App system-assigned identity (holds only the start role on the VM)."
  value       = azurerm_logic_app_workflow.start.identity[0].principal_id
}

output "subnet_id" {
  description = "Subnet of the VM NIC (created here or the one passed in)."
  value       = local.subnet_id
}
