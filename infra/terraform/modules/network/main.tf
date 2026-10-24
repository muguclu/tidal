# Private networking (T-404, SPEC-06 section 4, ADR-0006). Everything is gated on var.enabled: with the flag off the module
# creates no resource and every output is null or empty, so it costs nothing.

locals {
  on   = var.enabled
  tags = merge(var.tags, { component = var.component })

  # One /16 split into the five SPEC-06 subnets. snet-aca is a /23 because the Container Apps environment is
  # Consumption-only (no workload profiles), which needs at least a /23 and no delegation.
  subnets = {
    aks = cidrsubnet(var.address_space, 8, 0)   # /24 (10.x.0.0): nodes only, Azure CNI Overlay keeps pods out of the VNet
    aca = cidrsubnet(var.address_space, 7, 1)   # /23 (10.x.2.0)
    app = cidrsubnet(var.address_space, 11, 32) # /27 (10.x.4.0): App Service B1, one instance
    pe  = cidrsubnet(var.address_space, 11, 33) # /27 (10.x.4.32): three private endpoints
    vm  = cidrsubnet(var.address_space, 12, 80) # /28 (10.x.5.0): the legacy VM NIC
  }

  # Private DNS zone per private endpoint sub-resource (public cloud names).
  pe_services = {
    blob  = { zone = "privatelink.blob.core.windows.net", subresource = "blob" }
    sql   = { zone = "privatelink.database.windows.net", subresource = "sqlServer" }
    vault = { zone = "privatelink.vaultcore.azure.net", subresource = "vault" }
  }
  pe = local.on ? { for k, v in local.pe_services : k => v if contains(keys(var.private_endpoint_targets), k) } : {}

  nat = local.on && var.vm_outbound == "nat_gateway"
  aca = local.on && var.container_apps_environment != null
}

resource "azurerm_virtual_network" "this" {
  count = local.on ? 1 : 0

  name                = "vnet-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = [var.address_space]
  tags                = local.tags
}

# --- Subnets -------------------------------------------------------------------------------------------------------

resource "azurerm_subnet" "aks" {
  # checkov:skip=CKV2_AZURE_31: deferred; NIC-level NSG managed by AKS; no public node IPs (a subnet NSG would fight the rules AKS rewrites).
  count = local.on ? 1 : 0

  name                 = "snet-aks"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [local.subnets.aks]
}

resource "azurerm_subnet" "aca" {
  # checkov:skip=CKV2_AZURE_31: a Consumption-only Container Apps environment needs platform-managed outbound rules (ACR, MCR, Azure Monitor) that a subnet NSG would have to track. Inbound is internal only (internal load balancer).
  count = local.on ? 1 : 0

  name                 = "snet-aca"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [local.subnets.aca]
}

resource "azurerm_subnet" "app" {
  count = local.on ? 1 : 0

  name                 = "snet-app"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [local.subnets.app]

  delegation {
    name = "appservice"

    service_delegation {
      name    = "Microsoft.Web/serverFarms"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_subnet" "pe" {
  count = local.on ? 1 : 0

  name                              = "snet-pe"
  resource_group_name               = var.resource_group_name
  virtual_network_name              = azurerm_virtual_network.this[0].name
  address_prefixes                  = [local.subnets.pe]
  private_endpoint_network_policies = "Enabled"
  default_outbound_access_enabled   = false
}

resource "azurerm_subnet" "vm" {
  count = local.on ? 1 : 0

  name                 = "snet-vm"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [local.subnets.vm]

  # Explicit outbound (public IP on the NIC or the NAT gateway below) replaces the platform default outbound IP. No
  # Blob service endpoint: the Blob private endpoint serves the exporter, and a service endpoint cannot reach a storage
  # account whose public access is disabled.
  default_outbound_access_enabled = false
}

# --- NSGs (inbound closed; the VM module owns the NIC NSG with the outbound allow list) ------------------------------

resource "azurerm_network_security_group" "pe" {
  count = local.on ? 1 : 0

  name                = "nsg-tidal-pe-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = local.tags

  security_rule {
    name                       = "AllowVnetHttpsSql"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["443"]
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "VirtualNetwork"
  }

  # SQL (TDS 1433) only from the workloads that talk to it; the VM and anything else in the VNet cannot.
  security_rule {
    name                       = "AllowSqlFromWorkloads"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "1433"
    source_address_prefixes    = [local.subnets.aks, local.subnets.aca, local.subnets.app]
    destination_address_prefix = local.subnets.pe
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

# The integration subnet carries outbound traffic from the web app only; nothing connects inbound to it.
resource "azurerm_network_security_group" "app" {
  count = local.on ? 1 : 0

  name                = "nsg-tidal-app-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = local.tags

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

# Subnet level only closes inbound; the NIC NSG in modules/vm_legacy holds the outbound allow list.
resource "azurerm_network_security_group" "vm" {
  count = local.on ? 1 : 0

  name                = "nsg-tidal-vm-subnet-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = local.tags

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "pe" {
  count = local.on ? 1 : 0

  subnet_id                 = azurerm_subnet.pe[0].id
  network_security_group_id = azurerm_network_security_group.pe[0].id
}

resource "azurerm_subnet_network_security_group_association" "app" {
  count = local.on ? 1 : 0

  subnet_id                 = azurerm_subnet.app[0].id
  network_security_group_id = azurerm_network_security_group.app[0].id
}

resource "azurerm_subnet_network_security_group_association" "vm" {
  count = local.on ? 1 : 0

  subnet_id                 = azurerm_subnet.vm[0].id
  network_security_group_id = azurerm_network_security_group.vm[0].id
}

# --- Explicit outbound for snet-vm ------------------------------------------------------------------------------------
# One Standard static public IP in both modes. public_ip: the caller attaches it to the VM NIC (vm_legacy public_ip_id).
# nat_gateway: it belongs to the NAT gateway and the VM keeps no public address. A Standard IP accepts no inbound
# traffic until an NSG allows it, and the VM NIC NSG denies all inbound.

resource "azurerm_public_ip" "vm_outbound" {
  count = local.on ? 1 : 0

  name                = "pip-tidal-vm-outbound-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_nat_gateway" "vm" {
  count = local.nat ? 1 : 0

  name                    = "natgw-tidal-vm-${var.env}"
  resource_group_name     = var.resource_group_name
  location                = var.location
  sku_name                = "Standard"
  idle_timeout_in_minutes = 4
  tags                    = local.tags
}

resource "azurerm_nat_gateway_public_ip_association" "vm" {
  count = local.nat ? 1 : 0

  nat_gateway_id       = azurerm_nat_gateway.vm[0].id
  public_ip_address_id = azurerm_public_ip.vm_outbound[0].id
}

resource "azurerm_subnet_nat_gateway_association" "vm" {
  count = local.nat ? 1 : 0

  subnet_id      = azurerm_subnet.vm[0].id
  nat_gateway_id = azurerm_nat_gateway.vm[0].id
}

# --- Private DNS and private endpoints ------------------------------------------------------------------------------

resource "azurerm_private_dns_zone" "pe" {
  for_each = local.pe

  name                = each.value.zone
  resource_group_name = var.resource_group_name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "pe" {
  for_each = local.pe

  name                  = "link-${each.key}-${var.env}"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.pe[each.key].name
  virtual_network_id    = azurerm_virtual_network.this[0].id
  registration_enabled  = false
  tags                  = local.tags
}

resource "azurerm_private_endpoint" "this" {
  for_each = local.pe

  name                = "pe-tidal-${each.key}-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = azurerm_subnet.pe[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-${each.key}"
    private_connection_resource_id = var.private_endpoint_targets[each.key]
    subresource_names              = [each.value.subresource]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.pe[each.key].id]
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.pe]
}

# Internal Container Apps environment: *.<default_domain> must resolve to the environment's internal load balancer IP
# inside the VNet (App Service via snet-app, AKS pods, the VM).
resource "azurerm_private_dns_zone" "container_apps" {
  count = local.aca ? 1 : 0

  name                = var.container_apps_environment.default_domain
  resource_group_name = var.resource_group_name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "container_apps" {
  count = local.aca ? 1 : 0

  name                  = "link-aca-${var.env}"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.container_apps[0].name
  virtual_network_id    = azurerm_virtual_network.this[0].id
  registration_enabled  = false
  tags                  = local.tags
}

resource "azurerm_private_dns_a_record" "container_apps" {
  for_each = local.aca ? toset(["*", "@"]) : toset([])

  name                = each.value
  zone_name           = azurerm_private_dns_zone.container_apps[0].name
  resource_group_name = var.resource_group_name
  ttl                 = 300
  records             = [var.container_apps_environment.static_ip_address]
  tags                = local.tags
}
