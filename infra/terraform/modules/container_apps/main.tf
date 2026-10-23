locals {
  tags = merge(var.tags, { component = var.component })

  query_port = 8000

  # Shared plain settings first, then the module-owned keys, so a caller can never override TIDAL_ENV (ADR-0012).
  query_env = merge(var.common_env, var.query.extra_env, {
    TIDAL_ENV          = var.tidal_env
    TIDAL_SERVICE_NAME = "query"
    AZURE_CLIENT_ID    = var.query.identity_client_id
  })

  job_env = {
    for k, j in var.jobs : k => merge(var.common_env, j.extra_env, {
      TIDAL_ENV          = var.tidal_env
      TIDAL_SERVICE_NAME = j.service_name
      AZURE_CLIENT_ID    = j.identity_client_id
    })
  }
}

# Consumption-only environment (no workload profiles): billed per use, scale to zero (SPEC-07 section 2, ADR-0002).
resource "azurerm_container_app_environment" "this" {
  name                = "cae-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location

  # Checkov has no policies for the container app resource types (0 resources evaluated), so there are no skips here.
  # Deferred hardening: internal load balancer and VNet (T-404, SPEC-06 section 4).
  log_analytics_workspace_id = var.log_analytics_workspace_id

  # Zone redundancy needs a VNet subnet (T-404) and costs more; single-zone dev environment (provider default).

  tags = local.tags
}

resource "azurerm_container_app" "query" {
  name                         = "ca-tidal-query"
  resource_group_name          = var.resource_group_name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.query.identity_id]
  }

  # No admin user or password: the app identity pulls with AcrPull (assigned in the env composition).
  registry {
    server   = var.registry_server
    identity = var.query.identity_id
  }

  ingress {
    external_enabled           = var.query.external_ingress
    target_port                = local.query_port
    transport                  = "http"
    allow_insecure_connections = false

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.query.min_replicas
    max_replicas = var.query.max_replicas

    container {
      name    = "query"
      image   = var.image
      command = ["tidal-query"]
      cpu     = var.query.cpu
      memory  = var.query.memory

      dynamic "env" {
        for_each = local.query_env
        content {
          name  = env.key
          value = env.value
        }
      }

      # Probes follow the Dockerfile contract: the image has no HEALTHCHECK, query serves /health/live.
      startup_probe {
        transport               = "HTTP"
        port                    = local.query_port
        path                    = "/health/live"
        interval_seconds        = 5
        failure_count_threshold = 24
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = local.query_port
        path                    = "/health/live"
        interval_seconds        = 30
        failure_count_threshold = 3
      }
    }

    # SPEC-07 section 2: HTTP scale rule at 20 concurrent requests; zero replicas when idle.
    http_scale_rule {
      name                = "http-concurrency"
      concurrent_requests = tostring(var.query.concurrent_requests)
    }
  }

  tags = local.tags

  # The deploy workflow (T-311) rolls new image tags; Terraform only seeds the first revision.
  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }
}

resource "azurerm_container_app_job" "this" {
  for_each = var.jobs

  name                         = "caj-tidal-${each.key}"
  resource_group_name          = var.resource_group_name
  location                     = var.location
  container_app_environment_id = azurerm_container_app_environment.this.id

  replica_timeout_in_seconds = each.value.timeout_seconds
  replica_retry_limit        = each.value.retry_limit

  identity {
    type         = "UserAssigned"
    identity_ids = [each.value.identity_id]
  }

  registry {
    server   = var.registry_server
    identity = each.value.identity_id
  }

  dynamic "schedule_trigger_config" {
    for_each = each.value.trigger_type == "schedule" ? [1] : []
    content {
      cron_expression          = each.value.cron_expression
      parallelism              = 1
      replica_completion_count = 1
    }
  }

  dynamic "event_trigger_config" {
    for_each = each.value.trigger_type == "event" ? [1] : []
    content {
      parallelism              = 1
      replica_completion_count = 1

      scale {
        min_executions              = 0
        max_executions              = each.value.max_executions
        polling_interval_in_seconds = each.value.polling_interval

        rules {
          name             = "servicebus-${each.value.queue.queue_name}"
          custom_rule_type = "azure-servicebus"
          # KEDA authenticates with the job identity (identity_id), so there is no connection string or secret.
          # The identity needs Azure Service Bus Data Receiver on the queue (T-403, SPEC-06 section 2).
          identity_id = each.value.identity_id
          metadata = {
            namespace    = each.value.queue.namespace
            queueName    = each.value.queue.queue_name
            messageCount = tostring(each.value.queue.message_count)
          }
        }
      }
    }
  }

  template {
    container {
      name    = each.key
      image   = var.image
      command = each.value.command
      args    = each.value.args
      cpu     = each.value.cpu
      memory  = each.value.memory

      dynamic "env" {
        for_each = local.job_env[each.key]
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }
}
