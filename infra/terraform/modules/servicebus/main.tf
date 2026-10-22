locals {
  tags = merge(var.tags, { component = var.component })

  namespace_name = "sb-tidal-${var.env}-${var.name_suffix}"

  # ADR-0003: Basic unless the optional topic is requested; topics exist only from Standard.
  sku = var.enable_ingest_topic ? "Standard" : "Basic"

  # messaging-contract section 1 queue set; used by the check block below.
  contract_queues = toset(["backtest-jobs", "backtest-results", "tiering-jobs"])
}

# Warns at plan time when the queue map no longer matches the contract set.
check "queues_match_contract" {
  assert {
    condition     = toset(keys(var.queues)) == local.contract_queues
    error_message = "queues differ from messaging-contract section 1 (backtest-jobs, backtest-results, tiering-jobs). Change the contract (ADR + contract-change label) before diverging."
  }
}

resource "azurerm_servicebus_namespace" "this" {
  name                = local.namespace_name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = local.sku

  # Checkov skips (each justified):
  #checkov:skip=CKV_AZURE_199:No customer-managed key; CMK is Premium-only (ADR-0003), Microsoft-managed keys suffice for job messages
  #checkov:skip=CKV_AZURE_201:No customer-managed key; CMK is Premium-only (ADR-0003)
  #checkov:skip=CKV_AZURE_204:Public endpoint stays on by design (ADR-0006): a private endpoint needs Premium; access is Entra ID + RBAC with TLS 1.2
  #checkov:skip=CKV_AZURE_202:No managed identity needed: the namespace does not call other resources (no CMK, no capture)
  #checkov:skip=CKV_AZURE_218:Network rule sets and Premium features are not available on Basic
  minimum_tls_version = "1.2"

  # Entra ID + RBAC only: no SAS keys (SPEC-06 section 2). Role assignments are T-403.
  local_auth_enabled = false

  # ADR-0006: Service Bus stays public behind RBAC and TLS 1.2; a private endpoint requires Premium.
  public_network_access_enabled = true

  tags = local.tags
}

resource "azurerm_servicebus_queue" "this" {
  for_each = var.queues

  name         = each.key
  namespace_id = azurerm_servicebus_namespace.this.id

  lock_duration                        = each.value.lock_duration
  max_delivery_count                   = each.value.max_delivery_count
  default_message_ttl                  = each.value.default_message_ttl
  dead_lettering_on_message_expiration = each.value.dead_lettering_on_message_expiration

  # Basic has no sessions (ADR-0003; tiering concurrency uses sp_getapplock) and no duplicate detection
  # (messaging-contract section 4: consumers are idempotent).
  requires_session             = false
  requires_duplicate_detection = false
}

resource "azurerm_servicebus_topic" "ingest_events" {
  count = var.enable_ingest_topic ? 1 : 0

  name         = "ingest-events"
  namespace_id = azurerm_servicebus_namespace.this.id
}

resource "azurerm_servicebus_subscription" "ingest_events" {
  for_each = var.enable_ingest_topic ? toset(["stats", "quality-alerts"]) : toset([])

  name               = each.key
  topic_id           = azurerm_servicebus_topic.ingest_events[0].id
  max_delivery_count = 10
}
