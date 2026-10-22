locals {
  tags = merge(var.tags, { component = var.component })

  # Project is fixed to tidal (tags validation enforces it); alphanumeric only as ACR requires.
  registry_name = "acrtidal${var.env}${var.name_suffix}"

  # Tag filters passed to `acr purge --filter '<repo regex>:<tag regex>'`. --filter selects TAGS TO DELETE, so the
  # tag regex must never be broad: a `.*` here would delete v* release tags.
  #  - untagged_step_tag_filter matches no tag at all (a tag is never empty), so step 1 removes only manifests
  #    that are untagged and older than the cutoff. (`--untagged-only` would say this directly, but support in the
  #    acr-cli bundled with ACR Tasks is UNVERIFIED, so the no-match filter is used; it works on every version.)
  #  - sha_step_tag_filter matches only CI commit tags; their manifests are removed too once they are untagged.
  untagged_step_tag_filter = "^$"
  sha_step_tag_filter      = "^sha-.*$"

  # Task definition. v* release tags (and every non-sha tag) never match a filter, so they are kept forever.
  # Quotes protect the regex from the shell. {{.Run.Registry}} is an ACR Tasks run variable; the run authenticates
  # with the task run token, so no identity or credential is configured.
  purge_task_yaml = <<-YAML
    version: v1.1.0
    steps:
      - cmd: acr purge --registry {{.Run.Registry}} --filter '${var.purge_repository_regex}:${local.untagged_step_tag_filter}' --untagged --ago ${var.untagged_retention_days}d
        disableWorkingDirectoryOverride: true
        timeout: 3600
      - cmd: acr purge --registry {{.Run.Registry}} --filter '${var.purge_repository_regex}:${local.sha_step_tag_filter}' --untagged --ago ${var.sha_tag_retention_days}d
        disableWorkingDirectoryOverride: true
        timeout: 3600
  YAML
}

# Guard: refuse a broad tag filter that would delete release tags.
check "tag_filters_are_narrow" {
  assert {
    condition     = !contains([".*", "^.*$", ".+", "^.+$", ""], local.sha_step_tag_filter) && !contains([".*", "^.*$", ".+", "^.+$"], local.untagged_step_tag_filter)
    error_message = "A purge tag filter of .* would delete v* release tags. Keep the filters narrow."
  }
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

  # No identity and no registry_credential: {{.Run.Registry}} tasks authenticate with the run token.

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
