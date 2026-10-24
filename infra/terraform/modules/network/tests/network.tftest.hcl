# Offline plan tests (mock provider, no credentials): flag off creates nothing, flag on creates the expected set.
# Run: terraform init -backend=false && terraform test   (from modules/network)

mock_provider "azurerm" {}

variables {
  env                 = "dev"
  resource_group_name = "rg-tidal-dev"
  tags = {
    project     = "tidal"
    env         = "dev"
    owner       = "platform-team"
    cost_center = "tidal-portfolio"
    component   = "network"
    park        = "false"
  }
  private_endpoint_targets = {
    blob  = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.Storage/storageAccounts/sttidaldevtest"
    sql   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.Sql/servers/sql-tidal-dev-test"
    vault = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.KeyVault/vaults/kv-tidal-dev-test"
  }
  container_apps_environment = {
    default_domain    = "nice-sea-123.westeurope.azurecontainerapps.io"
    static_ip_address = "10.40.2.10"
  }
}

run "flag_off_creates_nothing" {
  command = plan

  variables {
    enabled = false
  }

  assert {
    condition     = length(azurerm_virtual_network.this) + length(azurerm_subnet.vm) + length(azurerm_public_ip.vm_outbound) + length(azurerm_nat_gateway.vm) == 0
    error_message = "flag off must create no network resource"
  }

  assert {
    condition     = length(azurerm_private_endpoint.this) + length(azurerm_private_dns_zone.pe) + length(azurerm_private_dns_zone.container_apps) == 0
    error_message = "flag off must create no private endpoint or DNS zone"
  }

  assert {
    condition     = output.aks_subnet_id == null && output.aca_subnet_id == null && output.app_subnet_id == null && output.vm_subnet_id == null && output.vm_public_ip_id == null
    error_message = "flag off outputs must be null"
  }
}

run "flag_on_public_ip" {
  command = plan

  variables {
    enabled = true
  }

  assert {
    condition     = length(azurerm_subnet.aks) + length(azurerm_subnet.aca) + length(azurerm_subnet.app) + length(azurerm_subnet.pe) + length(azurerm_subnet.vm) == 5
    error_message = "five subnets expected"
  }

  assert {
    condition     = length(azurerm_private_endpoint.this) == 3 && length(azurerm_private_dns_zone.pe) == 3 && length(azurerm_private_dns_zone_virtual_network_link.pe) == 3
    error_message = "blob, sql and vault each need an endpoint, a zone and a link"
  }

  assert {
    condition     = length(azurerm_public_ip.vm_outbound) == 1 && length(azurerm_nat_gateway.vm) == 0
    error_message = "public_ip mode: one IP, no NAT gateway"
  }

  assert {
    condition     = azurerm_subnet.vm[0].default_outbound_access_enabled == false && azurerm_subnet.vm[0].service_endpoints == null
    error_message = "snet-vm: default outbound off, no service endpoint"
  }

  assert {
    condition     = length([for n in [azurerm_network_security_group.pe[0], azurerm_network_security_group.app[0], azurerm_network_security_group.vm[0]] : n if length([for r in n.security_rule : r if r.direction == "Inbound" && r.access == "Allow" && r.source_address_prefix == "*"]) > 0]) == 0
    error_message = "no NSG may allow inbound from any source"
  }

  assert {
    condition     = length(azurerm_network_security_group.vm[0].security_rule) == 1 && output.pe_subnet_cidr == "10.40.4.32/27"
    error_message = "subnet NSG for snet-vm only denies inbound; pe prefix output known"
  }

  assert {
    condition     = length(azurerm_private_dns_zone.container_apps) == 1 && length(azurerm_private_dns_a_record.container_apps) == 2
    error_message = "internal Container Apps zone with wildcard and apex records"
  }
}

run "flag_on_nat_gateway" {
  command = plan

  variables {
    enabled     = true
    vm_outbound = "nat_gateway"
  }

  assert {
    condition     = length(azurerm_nat_gateway.vm) == 1 && length(azurerm_subnet_nat_gateway_association.vm) == 1 && length(azurerm_nat_gateway_public_ip_association.vm) == 1
    error_message = "nat_gateway mode: gateway, IP association, subnet association"
  }

  assert {
    condition     = output.vm_public_ip_id == null
    error_message = "nat_gateway mode: no public IP for the VM NIC"
  }
}
