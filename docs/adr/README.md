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
