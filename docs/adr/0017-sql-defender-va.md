# ADR-0017: Defender for SQL and Vulnerability Assessment are off by default; server auditing goes to Log Analytics

- Status: Accepted
- Date: 2026-10-24
- Deciders: Müge Güçlü (+ orchestrator, infra-engineer)
- Related: SPEC-06, SPEC-07 §2, ADR-0011, T-120, T-123, T-407, checkov CKV_AZURE_23/24, CKV2_AZURE_2

## Context
SPEC-06 needs an audit trail for the SQL server. Auditing can go to Storage, Log Analytics or Event Hubs. Defender for
SQL (threat detection) and Vulnerability Assessment (VA) are paid. The database is on the free offer (about 0 EUR) with
serverless auto-pause, and the whole dev environment composes to about 7.33 EUR/month at its ceiling. The Defender meter
(Microsoft Defender for SQL, "Standard Node") is 13.1996 EUR per server-month in westeurope (Retail Prices API,
2026-10-24), charged whether or not the database is paused. Model: `docs/finops/deltas/T-407_model.py`.

## Options
1. **Auditing to Log Analytics only; Defender and VA behind flags, off.** Audit ingestion for the dev action groups is
   about 0.0001 GB/day (0.1 % of the 0.2 GB/day cap), 0 EUR. Defender and VA cost 0 EUR.
2. **Auditing plus Defender and Express VA always on.** +13.20 EUR/month (+180 % on the dev ceiling) for a database that
   is paused most of the month and holds synthetic data only (ADR-0013). Scans can also wake the database.
3. **Auditing to a Storage account.** Needs the server identity and a storage account with its own lifecycle; the
   workspace already exists and is queryable (KQL). More moving parts for no gain.

## Decision
Option 1. Server auditing uses `azurerm_mssql_server_extended_auditing_policy` with `log_monitoring_enabled`, a diagnostic
setting for `SQLSecurityAuditEvents` on `master`, and a low-volume action-group set (failed logins and security-relevant
changes; no BATCH_COMPLETED). Defender (`enable_defender_for_sql`) and Express VA (`enable_vulnerability_assessment`)
are implemented behind flags with default `false`; prod, or a one-day demo, flips them (about 0.44 EUR for one day,
assuming daily proration).

Checkov: CKV_AZURE_23 is skipped on the server because the check only recognises the deprecated inline block and the
policy resource exists. CKV_AZURE_24 asks for 90 days or more of storage retention; the sink is the workspace with 30 days
(SPEC-07). CKV2_AZURE_2 (VA) is skipped because VA is behind the flag. All three skips carry these reasons in `main.tf`.

## Consequences
The dev environment has an audit trail at 0 EUR but no threat detection or vulnerability scanning; this is a conscious
portfolio-cost trade, written down here. Revisit if real data enters the system or the environment goes beyond dev.
Audit retention is 30 days; longer retention would need a storage sink or a workspace change. The auto-pause behaviour with
auditing on is not verified until the first dev apply.
