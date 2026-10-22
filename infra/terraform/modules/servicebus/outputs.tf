output "id" {
  description = "Resource ID of the Service Bus namespace."
  value       = azurerm_servicebus_namespace.this.id
}

output "name" {
  description = "Namespace name."
  value       = azurerm_servicebus_namespace.this.name
}

output "fqdn" {
  description = "Fully qualified namespace host name, maps to TIDAL_SERVICEBUS__FQDN (<name>.servicebus.windows.net)."
  value       = "${azurerm_servicebus_namespace.this.name}.servicebus.windows.net"
}

output "sku" {
  description = "Effective namespace SKU (Basic, or Standard when enable_ingest_topic is true)."
  value       = azurerm_servicebus_namespace.this.sku
}

output "queue_ids" {
  description = "Map of queue name to resource ID; T-403 scopes queue-level data roles (Sender/Receiver) to these per SPEC-06 section 2."
  value       = { for k, q in azurerm_servicebus_queue.this : k => q.id }
}

output "topic_id" {
  description = "Resource ID of the ingest-events topic, or null when enable_ingest_topic is false."
  value       = one(azurerm_servicebus_topic.ingest_events[*].id)
}

output "subscription_ids" {
  description = "Map of ingest-events subscription name to resource ID (empty when enable_ingest_topic is false)."
  value       = { for k, s in azurerm_servicebus_subscription.ingest_events : k => s.id }
}
