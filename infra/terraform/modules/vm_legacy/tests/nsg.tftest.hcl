# Offline plan test (mock provider): the VM NIC NSG must never allow inbound traffic (ADR-0018).
# Run from modules/vm_legacy: terraform init -backend=false && terraform test

mock_provider "azurerm" {}

variables {
  env                   = "dev"
  resource_group_name   = "rg-tidal-dev"
  component             = "legacy-vm"
  identity_id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-legacy-vm"
  identity_principal_id = "00000000-0000-0000-0000-000000000001"
  admin_ssh_public_key  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPonPDcgqfrDgzr8qQD02ndFN+hJ+cQayw0l3D+9Hml0 test"
  custom_data           = "#cloud-config"
  tags = {
    project     = "tidal"
    env         = "dev"
    owner       = "platform-team"
    cost_center = "tidal-portfolio"
    component   = "legacy-vm"
    park        = "false"
  }
}

run "no_inbound_allow_default" {
  command = plan

  assert {
    condition     = length([for r in azurerm_network_security_group.this.security_rule : r if r.direction == "Inbound" && r.access == "Allow"]) == 0
    error_message = "the NIC NSG must have no Inbound/Allow rule"
  }
}

run "no_inbound_allow_with_public_ip_and_pe_rule" {
  command = plan

  variables {
    use_external_subnet          = true
    subnet_id                    = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-vm"
    public_ip_id                 = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.Network/publicIPAddresses/pip"
    private_endpoint_subnet_cidr = "10.40.4.32/27"
  }

  assert {
    condition     = length([for r in azurerm_network_security_group.this.security_rule : r if r.direction == "Inbound" && r.access == "Allow"]) == 0
    error_message = "the NIC NSG must have no Inbound/Allow rule even with a public IP on the NIC"
  }

  assert {
    condition     = length([for r in azurerm_network_security_group.this.security_rule : r if r.direction == "Outbound" && r.destination_address_prefix == "10.40.4.32/27"]) == 1
    error_message = "VNet egress must be limited to the snet-pe prefix"
  }
}
