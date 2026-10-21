# SPEC-10 — Observability

Owner agents: `platform-engineer`, `finops-analyst` (cost dashboards) · Tasks: T-505, T-506

## 1. Signals
- **Logs:** structlog JSON → stdout → Container Insights / App Service logs → Log Analytics.
  Mandatory fields: `ts, level, service, version, correlation_id, principal_oid (hashed), event`.
- **Traces:** OpenTelemetry (`azure-monitor-opentelemetry`), sampling 20 % (100 % on errors).
- **Metrics (custom):** `tidal.query.rows`, `tidal.query.bytes_scanned`, `tidal.query.tier`,
  `tidal.backtest.duration_ms`, `tidal.tiering.rows_moved`, `tidal.ingest.rows_written`.

## 2. Dashboards (Azure Workbook in Terraform)
1. Platform health: API p50/p95/p99, error rate, dependency failures, SB queue depth & DLQ count.
2. Batch: KEDA replicas vs queue depth, node count per pool, spot evictions.
3. Data: rows per tier, SQL space, Blob capacity per tier, ingest freshness (age of latest bar per instrument).
4. Cost: daily cost by `component` tag, € per backtest, € per 1k queries.

## 3. Alerts (action group: email)
API 5xx > 2 % 10 min · DLQ > 0 · ingest freshness > 36 h on a weekday · budget 80 % · LA daily cap reached ·
spot eviction rate > 30 %/h.

## 4. Acceptance criteria
- [ ] One request traced end-to-end API → query svc → Blob, visible in App Insights transaction search.
- [ ] Workbook deployed by Terraform; screenshots in README.
- [ ] Each alert fired once in a test and recorded.
