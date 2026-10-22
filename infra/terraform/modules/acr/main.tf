locals {
  tags = merge(var.tags, { component = var.component })

  # Project is fixed to tidal (tags validation enforces it); alphanumeric only as ACR requires.
  registry_name = "acrtidal${var.env}${var.name_suffix}"

  # Task definition. Two steps: (1) untagged manifests past retention; (2) sha-* CI tags past retention.
  # v* release tags and any other tag never match a filter, so they are kept forever.
  # Quotes protect the regex from the shell. {{.Run.Registry}} is an ACR Tasks run variable.
  purge_task_yaml = <<-YAML
    version: v1.1.0
    steps:
      - cmd: acr purge --registry {{.Run.Registry}} --filter '${var.purge_repository_regex}:.*' --untagged --ago ${var.untagged_retention_days}d
        disableWorkingDirectoryOverride: true
        timeout: 3600
      - cmd: acr purge --registry {{.Run.Registry}} --filter '${var.purge_repository_regex}:sha-.*' --ago ${var.sha_tag_retention_days}d --untagged
        disableWorkingDirectoryOverride: true
        timeout: 3600
  YAML
}

resource "azurerm_container_registry" "this" {
  name                = local.registry_name
  resource_group_name = var.resource_group_name
  location            = var.location

  # Checkov skips (each justified; all are Premium-only or not applicable on Basic):
  #checkov:skip=CKV_AZURE_233:Zone redundancy is Premium-only; single-region dev registry
  #checkov:skip=CKV_AZURE_167:Retention policy for untagged manifests is Premium-only; the weekly ACR purge task below does this on Basic
  #checkov:skip=CKV_AZURE_165:Geo-replication is Premium-only (SPEC-07 section 2: Basic)
  #checkov:skip=CKV_AZURE_166:Image quarantine is Premium-only
  #checkov:skip=CKV_AZURE_164:Content trust (signed images) is Premium-only
  #checkov:skip=CKV_AZURE_163:Defender for Cloud image scanning is a subscription-level plan, not a registry setting; out of scope for this module
  #checkov:skip=CKV_AZURE_139:Public endpoint stays on by design: private endpoints and network rules need Premium; access is Entra ID + RBAC only (admin and anonymous pull off)
  #checkov:skip=CKV_AZURE_237:Dedicated data endpoints are Premium-only
  #checkov:skip=CKV_AZURE_160:Customer-managed key encryption needs Premium; Microsoft-managed keys suffice
  sku = "Basic"

  # SPEC-06 section 2: admin user disabled, pulls only via managed identity (AcrPull/AcrPush role assignments: T-403/T-306).
  admin_enabled = false

  # Basic has no private endpoint and no network rule set, so the public endpoint cannot be closed.
  # Mitigation: no admin, no anonymous pull, Entra ID + RBAC only.
  public_network_access_enabled = true
  anonymous_pull_enabled        = false

  # Deliberately NOT set (Premium-only, rejected by the service on Basic): retention_policy_in_days,
  # trust_policy_enabled, georeplications, network_rule_set, zone_redundancy_enabled, export_policy_enabled,
  # quarantine_policy_enabled, data_endpoint_enabled, encryption.

  tags = local.tags

  lifecycle {
    precondition {
      condition     = can(regex("^[a-zA-Z0-9]{5,50}$", local.registry_name))
      error_message = "Registry name must be alphanumeric and 5 to 50 characters."
    }
  }
}

# Weekly hygiene (SPEC-07 section 2, SPEC-09 C9). Timer-triggered only: no source trigger, no base-image trigger.
resource "azurerm_container_registry_task" "purge" {
  name                  = "weekly-purge"
  container_registry_id = azurerm_container_registry.this.id
  enabled               = true
  timeout_in_seconds    = 3600

  # No credentials: the task authenticates with its own system-assigned identity.
  identity {
    type = "SystemAssigned"
  }

  platform {
    os = "Linux"
  }

  encoded_step {
    task_content = local.purge_task_yaml
  }

  timer_trigger {
    name     = "weekly"
    schedule = var.purge_schedule
    enabled  = true
  }

  tags = local.tags
}

# The task identity needs delete rights, scoped to this registry only (least privilege).
resource "azurerm_role_assignment" "purge_task_acr_delete" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrDelete"
  principal_id         = azurerm_container_registry_task.purge.identity[0].principal_id
}
