# servicebus

One Service Bus namespace (**Basic**) with the three queues of messaging-contract section 1:
`backtest-jobs` (lock PT5M, max delivery 3, DLQ on expiry, TTL P1D), `backtest-results` (max delivery 5, DLQ on expiry),
`tiering-jobs` (max delivery 3). The `queues` variable defaults equal the contract; a `check` block warns when the
set of queue names drifts from it.

- `enable_ingest_topic = true` (default false) switches the namespace to **Standard** (topics are not available on Basic) and adds topic `ingest-events` with subscriptions `stats` and `quality-alerts`. This adds a fixed monthly base charge; see `docs/finops/deltas/T-124.md`. Basic → Standard updates in place; Standard → Basic can fail while the topic exists (remove it first). Check with `make tf-plan` before flipping the flag.
- Security: TLS 1.2, `local_auth_enabled = false` (no SAS keys; Entra ID + RBAC only). The public endpoint stays on per ADR-0006 because a private endpoint needs Premium.
- Basic has no sessions and no duplicate detection: `requires_session` and `requires_duplicate_detection` are false (ADR-0003, messaging-contract section 4); consumers are idempotent.
- RBAC (T-403): scope Sender/Receiver data roles to `queue_ids["<name>"]`, per SPEC-06 section 2. Role assignments are not in this module.
- `fqdn` maps to `TIDAL_SERVICEBUS__FQDN`.
- No `prevent_destroy`: `terraform destroy` must remove everything except bootstrap (SPEC-07 section 6).
- Checkov skips (inline, justified): double encryption and CMK (CKV_AZURE_199, CKV_AZURE_201; Premium-only), public endpoint (CKV_AZURE_204; ADR-0006), managed identity (CKV_AZURE_202).

Usage: see `examples/basic` (`-var enable_ingest_topic=true` for the Standard variant).

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_servicebus_namespace.this` | resource |
| `azurerm_servicebus_queue.this` | resource (for_each over `queues`) |
| `azurerm_servicebus_topic.ingest_events` | resource (count 0 by default) |
| `azurerm_servicebus_subscription.ingest_events` | resource (`stats`, `quality-alerts`; none by default) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group for the namespace. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `name_suffix` | Required suffix (3 to 8 lowercase alphanumerics) for the globally unique name `sb-tidal-<env>-<suffix>`. | `string` | n/a | yes |
| `enable_ingest_topic` | Standard SKU plus `ingest-events` topic and subscriptions. | `bool` | `false` | no |
| `queues` | Queue settings keyed by name; defaults equal messaging-contract section 1. | `map(object)` | contract set | no |
| `tags` | Tag map from `module.resource_group.tags` (validated). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags`. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `id` | Namespace resource ID. |
| `name` | Namespace name. |
| `fqdn` | `<name>.servicebus.windows.net` (`TIDAL_SERVICEBUS__FQDN`). |
| `sku` | Effective SKU (Basic or Standard). |
| `queue_ids` | Map of queue name to ID (for T-403 queue-scoped RBAC). |
| `topic_id` | `ingest-events` topic ID, or null. |
| `subscription_ids` | Map of subscription name to ID (empty by default). |
<!-- END_TF_DOCS -->
