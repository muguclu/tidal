# Legacy VM, exporter and Logic App start (T-310). Everything VM-specific lives in this file so the shared main.tf,
# variables.tf and outputs.tf stay untouched (same pattern as aks.tf and apps.tf).

variable "enable_legacy_vm" {
  description = "Create the legacy B1s VM, its Logic App start and the exporter. false removes them (park lever; the disk is the only meter left while the VM is deallocated)."
  type        = bool
  default     = true
}

variable "legacy_vm_ssh_public_key" {
  description = "OpenSSH public key for the legacy VM admin user (required, no default: Azure validates the key format). Azure requires one; the VM has no public IP and no inbound rule, so it cannot be used to log in."
  type        = string

  validation {
    condition     = can(regex("^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp[0-9]+) ", var.legacy_vm_ssh_public_key))
    error_message = "legacy_vm_ssh_public_key must be an OpenSSH public key."
  }
}

variable "legacy_exporter_lookback_days" {
  description = "Days back from the run date covered by each CSV feed. A lookback of N days yields N+1 trade dates (the window starts at UTC midnight N days ago and includes today)."
  type        = number
  default     = 7

  validation {
    condition     = var.legacy_exporter_lookback_days >= 1 && var.legacy_exporter_lookback_days <= 366 && floor(var.legacy_exporter_lookback_days) == var.legacy_exporter_lookback_days
    error_message = "legacy_exporter_lookback_days must be an integer from 1 to 366."
  }
}

locals {
  legacy_exporter_dir = "${path.module}/../../../../legacy/exporter"
}

module "vm_legacy" {
  source = "../../modules/vm_legacy"
  count  = var.enable_legacy_vm ? 1 : 0

  env                 = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "legacy-vm"

  identity_id           = module.identity.identities["id-legacy-vm"].id
  identity_principal_id = module.identity.identities["id-legacy-vm"].principal_id
  admin_ssh_public_key  = var.legacy_vm_ssh_public_key

  # Flag on (network.tf): snet-vm has default outbound access off, so the NIC carries the outbound-only public IP
  # (network_vm_outbound = public_ip) or the subnet has a NAT gateway (nat_gateway). Flag off: both are null and the
  # module keeps its own VNet with platform default outbound access.
  use_external_subnet = var.enable_private_networking
  subnet_id           = module.network.vm_subnet_id
  public_ip_id        = module.network.vm_public_ip_id

  # TIDAL_ENV is set explicitly (ADR-0012). No secret is rendered: the exporter uses the managed identity.
  custom_data = templatefile("${local.legacy_exporter_dir}/cloud-init.yaml.tftpl", {
    tidal_env           = local.env
    storage_account_url = trimsuffix(module.storage.primary_blob_endpoint, "/")
    client_id           = module.identity.identities["id-legacy-vm"].client_id
    lookback_days       = var.legacy_exporter_lookback_days
    requirements        = file("${local.legacy_exporter_dir}/requirements.txt")
    script              = file("${local.legacy_exporter_dir}/export_daily_bars.py")
    unit                = file("${local.legacy_exporter_dir}/tidal-exporter.service")
  })
}

# SPEC-06 section 2 (id-legacy-vm): write the CSV feed to exports, scoped to that container. The exporter also needs to
# read the cold daily bars, so it gets Blob Data Reader on the marketdata container only (the matrix row lists exports
# alone; SPEC-06 needs the reader line added, see docs/finops/deltas/T-310.md). No SQL role: the cold Parquet is the source.
resource "azurerm_role_assignment" "legacy_vm_exports_contributor" {
  count = var.enable_legacy_vm ? 1 : 0

  scope                = module.storage.container_ids["exports"]
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.identity.identities["id-legacy-vm"].principal_id
}

resource "azurerm_role_assignment" "legacy_vm_marketdata_reader" {
  count = var.enable_legacy_vm ? 1 : 0

  scope                = module.storage.container_ids["marketdata"]
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = module.identity.identities["id-legacy-vm"].principal_id
}

output "legacy_vm_name" {
  description = "Name of the legacy VM (null when enable_legacy_vm is false); the park script targets it with az vm deallocate."
  value       = one(module.vm_legacy[*].vm_name)
}

output "legacy_vm_logic_app_name" {
  description = "Name of the Logic App that starts the VM daily (null when enable_legacy_vm is false); park disables it."
  value       = one(module.vm_legacy[*].logic_app_name)
}
