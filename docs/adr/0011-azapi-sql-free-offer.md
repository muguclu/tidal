# ADR-0011: Use the `azapi` provider only to enable the Azure SQL free offer

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator, from T-123)
- Related: CLAUDE.md §2.4–2.5, LOCAL-DEV §2, cost-model (Azure SQL row), SPEC-09 C6, T-123

## Context
The Azure SQL Database free offer (GP serverless, 100k vCore-s + 32 GB per month, per subscription) makes the
hot store ≈ €0 in the cloud window. It is chosen when the database is created. azurerm 4.81.0 (latest 4.x)
has no `use_free_limit` / `free_limit_exhaustion_behavior` arguments; the upstream PRs are still open. Without
it, 40 h/month active costs ≈ €14–44 at list price (T-123 delta, MODELLED from prices.yaml).

## Options
1. **Wait for azurerm** — no new tool, but the first apply may happen before the provider ships it; € ≈ 14–44/month.
2. **`azapi` provider, scoped to one resource** — `azapi_update_resource` sets `useFreeLimit` and
   `freeLimitExhaustionBehavior = AutoPause` on the database created by azurerm; schema-validated by azapi at
   `terraform validate`; € ≈ 0 within the allowance.
3. **Create the database out of band** — breaks "everything is IaC" (CLAUDE.md §2.4).

## Decision
Option 2, limited to the SQL module and behind a `use_free_offer` variable (default true in dev); remove it once
azurerm supports the arguments natively.

## Consequences
One more provider (`Azure/azapi ~> 2.x`) in the SQL module and env composition; pinned in lock files. Monitor
the azurerm changelog and replace the azapi resource with native arguments when available (state move, no
recreate). The free offer is a dev-only lever and is labelled as such in the cost report.
