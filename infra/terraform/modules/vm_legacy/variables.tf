variable "env" {
  description = "Environment name used in resource names (dev or prod)."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the VM, network and Logic App."
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
  description = "Value of the component tag for the resources in this module (for example legacy-vm); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}

variable "identity_id" {
  description = "Resource ID of the user-assigned managed identity id-legacy-vm attached to the VM."
  type        = string

  validation {
    condition     = can(regex("(?i)/providers/Microsoft.ManagedIdentity/userAssignedIdentities/", var.identity_id))
    error_message = "identity_id must be the resource ID of a user-assigned managed identity."
  }
}

variable "identity_principal_id" {
  description = "Object (principal) ID of id-legacy-vm; receives the custom role vm-self-deallocate on the VM only (SPEC-06 section 2)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.identity_principal_id))
    error_message = "identity_principal_id must be a GUID."
  }
}

variable "admin_ssh_public_key" {
  description = "SSH public key for the VM admin user. Password login is disabled and the VM has no public IP and no inbound rule, so the key is only a provisioning requirement of Azure; nobody can reach port 22."
  type        = string

  validation {
    condition     = can(regex("^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp[0-9]+) ", var.admin_ssh_public_key))
    error_message = "admin_ssh_public_key must be an OpenSSH public key (ssh-rsa, ssh-ed25519 or ecdsa-sha2-*)."
  }
}

variable "admin_username" {
  description = "Admin user name of the VM."
  type        = string
  default     = "tidaladmin"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{2,31}$", var.admin_username)) && !contains(["admin", "administrator", "root"], var.admin_username)
    error_message = "admin_username must be 3-32 lowercase letters, digits, - or _, start with a letter, and not be admin, administrator or root."
  }
}

variable "custom_data" {
  description = "Rendered cloud-init document (envs/dev/vm.tf renders legacy/exporter/cloud-init.yaml.tftpl). Must not contain secrets: it is readable from the VM metadata."
  type        = string

  validation {
    condition     = startswith(var.custom_data, "#cloud-config")
    error_message = "custom_data must be a cloud-init document starting with #cloud-config."
  }
}

variable "vm_size" {
  description = "VM size. Standard_B1s is the SPEC-07 section 2 choice (burstable, 1 vCPU / 1 GiB, enough for a CSV export that runs minutes per day)."
  type        = string
  default     = "Standard_B1s"

  validation {
    condition     = contains(["Standard_B1s", "Standard_B1ms", "Standard_B2s"], var.vm_size)
    error_message = "vm_size must be Standard_B1s, Standard_B1ms or Standard_B2s; anything larger needs a cost-model update."
  }
}

variable "os_disk_type" {
  description = "OS disk SKU. StandardSSD_LRS is the cheapest managed SSD; the 30 GiB Ubuntu image bills as an E4 disk."
  type        = string
  default     = "StandardSSD_LRS"

  validation {
    condition     = contains(["Standard_LRS", "StandardSSD_LRS"], var.os_disk_type)
    error_message = "os_disk_type must be Standard_LRS or StandardSSD_LRS (Premium disks are not justified for a daily batch job)."
  }
}

variable "encryption_at_host_enabled" {
  description = "Encrypt temp disk and caches at the host. Needs the Microsoft.Compute/EncryptionAtHost feature registered on the subscription, so it is off by default."
  type        = bool
  default     = false
}

variable "subnet_id" {
  description = "Existing subnet for the VM NIC (snet-vm from the network module, T-404). null creates a small private VNet and subnet in this module."
  type        = string
  default     = null
}

variable "vnet_address_space" {
  description = "Address space of the VNet created when subnet_id is null. Must not overlap the T-404 network."
  type        = string
  default     = "10.250.0.0/28"

  validation {
    condition     = can(cidrhost(var.vnet_address_space, 0))
    error_message = "vnet_address_space must be a valid CIDR block, e.g. 10.250.0.0/28."
  }
}

variable "start_enabled" {
  description = "Whether the Logic App recurrence is enabled. false is the park lever for the daily VM start (scripts/park.sh, T-313)."
  type        = bool
  default     = true
}

variable "start_time_zone" {
  description = "Windows time zone name used by the Logic App recurrence and the auto-shutdown schedule (SPEC-07 section 2 says CET)."
  type        = string
  default     = "W. Europe Standard Time"
}

variable "start_hour" {
  description = "Hour (0-23, in start_time_zone) at which the Logic App starts the VM. SPEC-07: 21:55."
  type        = number
  default     = 21

  validation {
    condition     = var.start_hour >= 0 && var.start_hour <= 23 && floor(var.start_hour) == var.start_hour
    error_message = "start_hour must be an integer from 0 to 23."
  }
}

variable "start_minute" {
  description = "Minute (0-59) at which the Logic App starts the VM."
  type        = number
  default     = 55

  validation {
    condition     = var.start_minute >= 0 && var.start_minute <= 59 && floor(var.start_minute) == var.start_minute
    error_message = "start_minute must be an integer from 0 to 59."
  }
}

variable "auto_shutdown_time" {
  description = "Daily auto-shutdown time as HHMM in start_time_zone (SPEC-07: 2000). Safety net if the exporter fails to deallocate."
  type        = string
  default     = "2000"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3])[0-5][0-9]$", var.auto_shutdown_time))
    error_message = "auto_shutdown_time must be HHMM, for example 2000."
  }
}
