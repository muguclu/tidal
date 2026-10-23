# Architecture Decision Records

New ADRs use [0000-template.md](0000-template.md). Breaking contract changes need an ADR
(data-contracts §8).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-contracts-first.md) | Record architecture decisions; contracts-first development | Accepted |
| [0002](0002-compute-targets.md) | One codebase, four compute targets (App Service, Container Apps, AKS, VM) | Accepted |
| [0003](0003-servicebus-basic.md) | Service Bus Basic queues instead of Standard topics | Accepted |
| [0004](0004-storage-format-tiering.md) | Epoch Parquet on flat-namespace Blob, tiering by data age via index tags, no Archive for market data | Accepted |
| [0005](0005-trunk-based.md) | Trunk-based development with short-lived branches | Accepted |
| [0006](0006-terraform-network.md) | Terraform over Bicep; no Service Bus private endpoint (Premium cost) | Accepted |
| [0007](0007-baseline-dependencies.md) | Baseline dependency set (declared up-front in T-001) | Accepted |
| 0008 | Threat model (STRIDE) | written in T-406 |
| [0009](0009-daily-bar-timestamp.md) | Daily bars stamped at UTC midnight of the trade date | Accepted |
| [0010](0010-marketdata-container-name.md) | Market-data Blob container is named `marketdata` | Accepted |
| [0011](0011-azapi-sql-free-offer.md) | azapi provider only to enable the Azure SQL free offer | Accepted |
| [0012](0012-tidal-env-required.md) | `TIDAL_ENV` is required (fail closed) | Accepted |
| [0013](0013-synthetic-only-data.md) | Synthetic-only data; EIA/FRED adapters stay as unwired reference code | Accepted |
| [0014](0014-tiering-versioning.md) | Versions and as-of reads when tiering meets rows that are already cold | Accepted |
| [0015](0015-minimal-index-tags.md) | Minimal blob index tags: `age_band` + `compacted`, tick parts only | Accepted |
| [0016](0016-worker-reads-via-query.md) | The AKS backtest worker reads market data through `tidal-query` (no SQL access) | Accepted |
