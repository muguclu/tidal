# budget

Action group `ag-tidal-<env>` (email receivers from `alert_emails`) and a monthly cost budget with notifications at
50, 80 and 100 % of **Actual** spend plus 100 % **Forecasted** (SPEC-07 section 2, PLANNING section 6). Each
notification goes to the action group only. The 80 % actual alert is the
SPEC-10 section 3 "budget 80 %" alert. The action group id (`action_group_id`) is reused by the T-506 platform alerts.

- Amount: default 60 (SPEC-07). Azure budgets have **no currency argument**; the amount is read in the subscription
  billing currency. On a EUR account 60 means EUR 60, on a USD account USD 60. The cloud-window credit alerts at USD
  50 / 100 / 150 (LOCAL-DEV section 2) are subscription-level, so use `credit_alert_amounts` for them.
- Time grain is `Monthly`; `start_date` is required, must be the first of a month at 00:00:00Z, and Azure rejects
  a start date before the first of the current month for a new budget.
- Scope trade-off (`budget_scope`, default `subscription`):
  - `subscription`: `azurerm_consumption_budget_subscription`. Covers all spend, including the AKS node resource
    group `MC_*`, the largest cost item (SPEC-09 C4), and the cloud-window credit. Needs subscription-level rights.
    Terraform apply is human-only in this project (SPEC-08 deploy.yml never applies), so this is not a CI concern.
  - `resource_group`: `azurerm_consumption_budget_resource_group`, scoped to `resource_group_id`. Ignores spend
    outside the group (`MC_*` is a separate group and is NOT counted), so it under-reports.
- `start_date`: Azure rejects start dates before the current month and a change would recreate the budget, so all
  budgets have `lifecycle { ignore_changes = [time_period[0].start_date] }`. A rebuilt environment (a new cloud
  window) must pass the current month. `end_date` and `credit_window_end_date` must be later than `start_date`.
- Notifications: sent through the action group only (`contact_groups`); no separate `contact_emails`, so each
  alert arrives once.
- Forecast alert: the 100 % Forecasted notification is kept, but Azure may reject forecast alerts on brand-new
  budgets (not enough usage history). The first apply verifies it; if rejected, retry later.
- Credit alerts: `credit_alert_amounts` (default empty), for example `[50, 100, 150]` (LOCAL-DEV section 2). Each
  amount creates an extra subscription budget `budget-tidal-<env>-credit-<amount>` with one 100 % Actual
  notification. They use the `Annually` grain with `end_date = credit_window_end_date` (required, within one year of
  `start_date`), so the budget period is the whole cloud window and spend accumulates against the amount rather than
  resetting monthly. Amounts are in the billing currency, so USD 50/100/150 only matches a USD-billed account.
- Tags: the action group is tagged. `azurerm_consumption_budget_*` and `azurerm_cost_anomaly_alert` have no `tags`
  argument in the azurerm schema, so they cannot be tagged.
- No email is hard-coded; the env composition supplies `alert_emails`. The example uses a reserved
  `example.invalid` placeholder.
- Checkov: 0 resources are evaluated; checkov 3.3.30 has no policies for these resource types. The tflint
  `prevent_destroy` rule is disabled so dev environments can be destroyed.
- Cost: EUR 0, see `docs/finops/deltas/T-125.md`.

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_monitor_action_group.this` | resource |
| `azurerm_consumption_budget_resource_group.this` | resource (when `budget_scope = "resource_group"`) |
| `azurerm_consumption_budget_subscription.this` | resource (when `budget_scope = "subscription"`) |
| `azurerm_consumption_budget_subscription.credit` | resource (one per `credit_alert_amounts` entry) |
| `azurerm_cost_anomaly_alert.this` | resource (when `enable_anomaly_alert`) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group for the action group. | `string` | n/a | yes |
| `resource_group_id` | Resource group ID; RG budget scope and source of the subscription ID. | `string` | n/a | yes |
| `alert_emails` | 1 to 10 distinct email addresses for the action group. | `list(string)` | n/a | yes |
| `action_group_short_name` | Action group short name (max 12 characters). | `string` | `"tidal"` | no |
| `budget_scope` | `subscription` or `resource_group`. | `string` | `"subscription"` | no |
| `amount` | Monthly budget in the billing currency. | `number` | `60` | no |
| `start_date` | First of a month, midnight UTC (RFC 3339). | `string` | n/a | yes |
| `end_date` | Optional period end (RFC 3339). | `string` | `null` | no |
| `credit_window_end_date` | Cloud window end; required with `credit_alert_amounts`; sets the Annually-grain credit budget period. | `string` | `null` | no |
| `credit_alert_amounts` | Extra subscription budgets with a 100 % Actual alert (billing currency). | `list(number)` | `[]` | no |
| `enable_anomaly_alert` | Create the subscription cost anomaly alert. | `bool` | `false` | no |
| `tags` | Tag map from `module.resource_group.tags` (validated). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags`. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `action_group_id` | Action group ID (reused by T-506). |
| `action_group_name` | Action group name. |
| `budget_id` | Main budget ID. |
| `credit_budget_ids` | Map of credit amount to budget ID. |
| `anomaly_alert_id` | Anomaly alert ID, or null. |
<!-- END_TF_DOCS -->
