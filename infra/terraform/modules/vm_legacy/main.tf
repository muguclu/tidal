locals {
  tags        = merge(var.tags, { component = var.component })
  vm_name     = "vm-tidal-legacy-${var.env}"
  create_net  = var.subnet_id == null
  subnet_id   = local.create_net ? azurerm_subnet.this[0].id : var.subnet_id
  compute_api = "2024-07-01"
}

# --- Network: private only ----------------------------------------------------------------------
# No public IP and no inbound rule. When T-404 provides snet-vm, pass subnet_id and the VNet below is not created.

resource "azurerm_virtual_network" "this" {
  count = local.create_net ? 1 : 0

  name                = "vnet-tidal-legacy-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = [var.vnet_address_space]
  tags                = local.tags
}

resource "azurerm_subnet" "this" {
  count = local.create_net ? 1 : 0

  name                 = "snet-vm"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.vnet_address_space]

  # The VM must reach Blob, management.azure.com (deallocate), PyPI and the Ubuntu mirrors. A NAT gateway
  # (about 30 EUR/month) or a public IP would cost more than the VM itself, so the subnet keeps the platform
  # default outbound access (no address of our own, no inbound). T-404 can replace this with a storage
  # service endpoint plus NAT, see docs/finops/deltas/T-310.md.
  default_outbound_access_enabled = true
}

resource "azurerm_network_security_group" "this" {
  name                = "nsg-tidal-legacy-${var.env}"
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

  security_rule {
    name                       = "AllowInternetWebOutbound"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443"]
    source_address_prefix      = "*"
    destination_address_prefix = "Internet"
  }

  security_rule {
    name                       = "DenyAllOtherOutbound"
    priority                   = 4000
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "this" {
  count = local.create_net ? 1 : 0

  subnet_id                 = azurerm_subnet.this[0].id
  network_security_group_id = azurerm_network_security_group.this.id
}

resource "azurerm_network_interface" "this" {
  name                = "nic-tidal-legacy-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = local.tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = local.subnet_id
    private_ip_address_allocation = "Dynamic"
  }
}

# On the NIC as well, so the rules also hold when a shared subnet is passed in.
resource "azurerm_network_interface_security_group_association" "this" {
  network_interface_id      = azurerm_network_interface.this.id
  network_security_group_id = azurerm_network_security_group.this.id
}

# --- VM -----------------------------------------------------------------------------------------

resource "azurerm_linux_virtual_machine" "this" {
  name                = local.vm_name
  resource_group_name = var.resource_group_name
  location            = var.location
  size                = var.vm_size
  tags                = local.tags

  admin_username                  = var.admin_username
  disable_password_authentication = true
  allow_extension_operations      = false
  encryption_at_host_enabled      = var.encryption_at_host_enabled
  network_interface_ids           = [azurerm_network_interface.this.id]
  custom_data                     = base64encode(var.custom_data)

  admin_ssh_key {
    username   = var.admin_username
    public_key = var.admin_ssh_public_key
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = var.os_disk_type
  }

  # Ubuntu 24.04 LTS (Python 3.12, matching the repository baseline).
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  # Managed boot diagnostics storage (no storage account to pay for or secure).
  boot_diagnostics {}
}

# Safety net: deallocate at the SPEC-07 time if the exporter could not do it itself. No notification (no webhook, no mail).
resource "azurerm_dev_test_global_vm_shutdown_schedule" "this" {
  virtual_machine_id    = azurerm_linux_virtual_machine.this.id
  location              = var.location
  enabled               = true
  daily_recurrence_time = var.auto_shutdown_time
  timezone              = var.start_time_zone
  tags                  = local.tags

  notification_settings {
    enabled = false
  }
}

# --- Custom roles, both scoped to this VM only (SPEC-06 section 2) -----------------------------

resource "azurerm_role_definition" "self_deallocate" {
  name        = "tidal-vm-self-deallocate-${var.env}"
  scope       = azurerm_linux_virtual_machine.this.id
  description = "Deallocate the legacy VM. Assigned to id-legacy-vm on the VM itself so the exporter can stop the compute meter."

  permissions {
    actions     = ["Microsoft.Compute/virtualMachines/deallocate/action"]
    not_actions = []
  }

  assignable_scopes = [azurerm_linux_virtual_machine.this.id]
}

resource "azurerm_role_assignment" "self_deallocate" {
  scope              = azurerm_linux_virtual_machine.this.id
  role_definition_id = azurerm_role_definition.self_deallocate.role_definition_resource_id
  principal_id       = var.identity_principal_id
}

resource "azurerm_role_definition" "start" {
  name        = "tidal-vm-start-${var.env}"
  scope       = azurerm_linux_virtual_machine.this.id
  description = "Start the legacy VM. Assigned to the Logic App managed identity on the VM itself."

  permissions {
    actions     = ["Microsoft.Compute/virtualMachines/start/action"]
    not_actions = []
  }

  assignable_scopes = [azurerm_linux_virtual_machine.this.id]
}

# --- Logic App (Consumption): daily start --------------------------------------------------------

resource "azurerm_logic_app_workflow" "start" {
  name                = "logic-tidal-vm-start-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  enabled             = var.start_enabled
  tags                = local.tags

  # Its own system-assigned identity, so the start permission is not shared with the VM identity.
  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "start" {
  scope              = azurerm_linux_virtual_machine.this.id
  role_definition_id = azurerm_role_definition.start.role_definition_resource_id
  principal_id       = azurerm_logic_app_workflow.start.identity[0].principal_id
}

resource "azurerm_logic_app_trigger_recurrence" "daily" {
  name         = "daily-vm-start"
  logic_app_id = azurerm_logic_app_workflow.start.id
  frequency    = "Day"
  interval     = 1
  time_zone    = var.start_time_zone

  schedule {
    at_these_hours   = [var.start_hour]
    at_these_minutes = [var.start_minute]
  }
}

# Built-in HTTP action (counts against the free 4,000 built-in actions per month, about 30 per month used).
resource "azurerm_logic_app_action_custom" "start_vm" {
  name         = "start-vm"
  logic_app_id = azurerm_logic_app_workflow.start.id

  body = jsonencode({
    type = "Http"
    inputs = {
      method = "POST"
      uri    = "https://management.azure.com${azurerm_linux_virtual_machine.this.id}/start?api-version=${local.compute_api}"
      authentication = {
        type     = "ManagedServiceIdentity"
        audience = "https://management.azure.com/"
      }
    }
    runAfter = {}
  })

  depends_on = [azurerm_logic_app_trigger_recurrence.daily, azurerm_role_assignment.start]
}
