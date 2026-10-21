# ADR-0003: Service Bus Basic queues instead of Standard topics

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [messaging-contract §1, §4](../contracts/messaging-contract.md), [SPEC-03 §2](../specs/SPEC-03-tiering.md),
  [cost-model](../finops/cost-model.md), [plan review #27](../reviews/2026-10-21-plan-review.md), ADR-0006, T-304, T-501

## Context
Backtest submissions, backtest results and tiering runs need a durable queue so that work survives
restarts and spot evictions, and so AKS can scale on queue depth (KEDA). The job lists **Service Bus**
explicitly. Publish/subscribe (topic `ingest-events`) is only a Phase 5 nice-to-have. Cost is a feature.

## Options
1. **Basic namespace: queues only.**
   - Pros: cheapest tier; covers all three required queues.
   - Cons: no topics, no sessions, no duplicate detection.
   - € impact: < €1/month (cost-model).
2. **Standard namespace: queues + topics + sessions + duplicate detection.**
   - Pros: pub/sub fan-out; sessions for per-table ordering; broker de-duplication.
   - Cons: fixed monthly base charge whether used or not.
   - € impact: base charge to be priced in T-501 (`prices.yaml`); not yet in the repo.

## Decision
Basic, because every feature Standard adds can be replaced by a few lines of deterministic code we
need anyway, at a fraction of the cost.

## Consequences
- Concurrency for tiering is guarded by SQL `sp_getapplock` per table instead of sessions.
- No broker de-duplication: workers skip work when `output_prefix/_SUCCESS` exists, and the result
  aggregator is idempotent on `job_id` (conditional update, terminal states counted once).
- Messages are capped at 200 KiB serialised; larger inputs go to Blob and the message carries a URL.
- Topic `ingest-events` stays behind Terraform flag `enable_ingest_topic` (default false); turning it
  on upgrades the namespace to Standard and must add a cost delta.
- No private endpoint for Service Bus (needs Premium); see ADR-0006.
- Monitor: DLQ depth, redelivery count, Service Bus operations (SPEC-09 §2).
- Revisit if pub/sub becomes a core feature or if duplicate results cause incorrect aggregates.
