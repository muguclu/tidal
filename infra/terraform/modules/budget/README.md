# budget

Action group `ag-tidal-<env>` (email receivers from `alert_emails`) and a monthly cost budget with notifications at
50, 80 and 100 % of **Actual** spend plus 100 % **Forecasted** (SPEC-07 section 2, PLANNING section 6). Each
notification goes to the action group and to the same addresses as budget contact emails. The 80 % actual alert is the
SPEC-10 section 3 "budget 80 %" alert. The action group id (`action_group_id`) is reused by the T-506 platform alerts.

- Amount: default 60 (SPEC-07). Azure budgets have **no currency argument**; the amount is read in the subscription
  billing currency. On a EUR account 60 means EUR 60, on a USD account USD 60. The cloud-window credit alerts at USD
  50 / 100 / 150 (LOCAL-DEV section 2) are subscription-level, so use `budget_scope = "subscription"` with
  `amount` set to the credit threshold for that, or a separate instance.
- Time grain is `Monthly`; `start_date` is required, must be the first of a month at 00:00:00Z, and Azure rejects
  a start date before the first of the current month for a new budget.
- Scope trade-off (`budget_scope`):
  - `resource_group` (default): `azurerm_consumption_budget_resource_group`. Fits SPEC-06 section 2 (CI holds
    Contributor on `rg-tidal-<env>` only) so CI can apply it, but it ignores spend outside the group (for example
    the AKS node resource group `MC_*`, which is a separate group and is NOT counted).
  - `subscription`: `azurerm_consumption_budget_subscription`. Covers all spend including `MC_*` and the credit;
    needs subscription-level rights, so a human applies it.
- Cost anomaly alert: `enable_anomaly_alert` (default false) creates `azurerm_cost_anomaly_alert` (present in azurerm
  4.81.0, verified with `terraform providers schema -json`). It is subscription-scoped and the CI identity lacks the
  rights, so a human applies it with the flag on.
- Tags: the action group is tagged. `azurerm_consumption_budget_*` and `azurerm_cost_anomaly_alert` have no `tags`
  argument in the azurerm schema, so they cannot be tagged.
- No email is hard-coded; the env composition supplies `alert_emails`. The example uses a reserved
  `example.invalid` placeholder.
- Checkov: 0 resources are evaluated; checkov 3.3.30 has no policies for these resource types. The tflint
  `prevent_destroy` rule is disabled so dev environments can be destroyed.
- Cost: EUR 0, see `docs/finops/deltas/T-125.md`.

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->
