locals {
  tags     = merge(var.tags, { component = var.component })
  app_name = "app-tidal-api-${var.env}-${var.name_suffix}"

  app_settings = merge(var.app_settings, {
    TIDAL_ENV          = var.tidal_env
    TIDAL_SERVICE_NAME = "api"
    AZURE_CLIENT_ID    = var.identity_client_id
    WEBSITES_PORT      = tostring(var.container_port)
    # The image is pulled from ACR with the managed identity; App Service must not mount a storage share.
    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
  })
}

resource "azurerm_service_plan" "this" {
  name                = "asp-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  os_type             = "Linux"
  sku_name            = var.sku_name

  # Checkov skips (each justified):
  #checkov:skip=CKV_AZURE_211:B1 is below the production SKUs the check expects; it is the SPEC-07 section 2 cost decision for a dev portfolio environment
  #checkov:skip=CKV_AZURE_212:A second instance doubles the B1 price; single instance is the SPEC-07 section 2 "smallest always-on" decision
  #checkov:skip=CKV_AZURE_225:Zone redundancy needs Premium v2/v3 and at least 3 instances; not affordable for dev (SPEC-07 section 2)
  worker_count = 1

  tags = local.tags
}

resource "azurerm_linux_web_app" "this" {
  name                = local.app_name
  resource_group_name = var.resource_group_name
  location            = var.location
  service_plan_id     = azurerm_service_plan.this.id

  https_only = true

  # Checkov skips (each justified):
  #checkov:skip=CKV_AZURE_13:App Service built-in authentication is not used: tidal-api validates Entra ID JWTs itself (SPEC-06 section 1) so that /v1/health/live stays open for the platform probe
  #checkov:skip=CKV_AZURE_17:Client certificates are not used; callers authenticate with Entra ID bearer tokens
  #checkov:skip=CKV_AZURE_88:No Azure Files mount; WEBSITES_ENABLE_APP_SERVICE_STORAGE is false
  #checkov:skip=CKV_AZURE_222:Public network access stays on until the private networking phase (T-404, SPEC-06 section 4); every route needs an Entra JWT
  public_network_access_enabled = true
  client_certificate_enabled    = false

  # Remote debugging, FTP and basic auth are closed.
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false

  virtual_network_subnet_id = var.vnet_integration_subnet_id

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  site_config {
    always_on           = true
    http2_enabled       = true
    minimum_tls_version = "1.2"
    ftps_state          = "Disabled"
    # Key Vault references resolve through the VNet when the integration subnet is set (private networking).
    vnet_route_all_enabled                        = var.vnet_integration_subnet_id != null
    remote_debugging_enabled                      = false
    health_check_path                             = var.health_check_path
    health_check_eviction_time_in_min             = 5
    container_registry_use_managed_identity       = true
    container_registry_managed_identity_client_id = var.identity_client_id

    # Recycle the worker when the API keeps returning 5xx (tflint azurerm_app_service_missing_auto_heal_setting).
    auto_heal_setting {
      trigger {
        status_code {
          count             = 10
          interval          = "00:05:00"
          status_code_range = "500-599"
        }
      }
      action {
        action_type                    = "Recycle"
        minimum_process_execution_time = "00:02:00"
      }
    }

    application_stack {
      docker_registry_url = "https://${var.registry_server}"
      docker_image_name   = var.image_name
    }
  }

  app_settings = local.app_settings

  # @Microsoft.KeyVault(...) settings are resolved with the user-assigned identity (there is no system-assigned one).
  key_vault_reference_identity_id = var.identity_id

  # Application and HTTP logs go to the app filesystem with a short retention: no extra meter, and it keeps the
  # 0.2 GB/day Log Analytics cap for OpenTelemetry. Detailed errors and failed request tracing are filesystem-only too. The 35 MB quota makes App Service stop writing when full.
  logs {
    detailed_error_messages = true
    failed_request_tracing  = true

    application_logs {
      file_system_level = "Warning"
    }

    http_logs {
      file_system {
        retention_in_days = var.log_retention_days
        retention_in_mb   = 35
      }
    }
  }

  tags = local.tags

  # The deploy workflow (T-311) moves the image tag.
  lifecycle {
    ignore_changes = [site_config[0].application_stack[0].docker_image_name]
  }
}
