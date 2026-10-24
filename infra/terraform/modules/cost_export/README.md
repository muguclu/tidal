# cost_export

Daily **Actual cost**, **month-to-date** CSV export of one resource group (`azurerm_resource_group_cost_management_export`) into a
private `cost-exports` container of the **existing** environment storage account (T-502, SPEC-09 section 2.1). The weekly report
(`src/tidal/finops/cost_report.py`, `.github/workflows/cost.yml`) reads it.

- **Why reuse the account:** a second account adds RBAC and tag surface for a few KB per day. A separate container keeps the reader
  role scoped to export data only.
- **Scope:** resource group, so no subscription rights are needed. The AKS node resource group `MC_*` is a separate group and is
  **not** exported (C4 spot node-hours are therefore NEEDS-CLOUD: a subscription-scope export, or the Azure Monitor node-hours metric).
- **Layout (written by Azure):** `cost-exports/<root_folder_path>/export-tidal-<env>-actual/<YYYYMMDD-YYYYMMDD>/<run id>/part_*.csv`
  plus `manifest.json`. Overwrite is on by default: one run id per month folder, replaced daily. Two runs happen per day on the
  first five days of a month (the second finalises the previous month).
- **Writing identity:** Cost Management creates a system-assigned identity for the export and grants it Storage Blob Data
  Contributor on the container, when the applying human has `Microsoft.Authorization/roleAssignments/write` (Owner). The account has
  shared key access off, so this path matters; verify on first apply (NEEDS-CLOUD). The account setting "Permitted scope for copy
  operations" must stay at its default (from any storage account).
- **Reader:** `reader_principal_id` (object ID of `sp-tidal-ci-plan`) gets **Storage Blob Data Reader on the container only**.
  Cost Management Reader was rejected: it exposes live cost queries for the whole scope and is broader than a CSV download needs.
  The same app already has Reader plus state Blob Reader (ADR-0008 R-18); this adds a third, still read-only, role. The assignment
  lives in `envs/dev`, so `docs/rbac/expected.json` needs one new row when the environment is applied (outside this task's files).
- **Private networking:** with `enable_private_networking = true` the account has no public endpoint; a GitHub-hosted runner cannot
  read it (same limitation as the Terraform plan job) and the export needs "trusted Azure services" access. Not handled here.
- **Cost:** 0.00 for the export itself (Cost Management exports are free); a few KB per month of Hot storage rounds to zero
  (`docs/finops/deltas/T-502.md`).
- `recurrence_start_date`: Azure may reject a past date on create. Apply is human-only; pass today or later if it does.
