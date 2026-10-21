# Messaging Contract — v1 (Azure Service Bus)

Code mirror: `src/tidal/contracts/messages.py`. JSON Schemas generated to
`docs/contracts/schemas/*.json` by `make contracts`.

## 1. Entities

| Entity | Type | Producer | Consumer | Settings |
|---|---|---|---|---|
| `backtest-jobs` | queue | API | AKS worker (KEDA ScaledJob) | lock 5 min (auto-renew), max delivery 3, DLQ on, TTL 24 h |
| `backtest-results` | queue | worker | API result aggregator | max delivery 5, DLQ on |
| `tiering-jobs` | queue | API admin / scheduler | migration job (Container Apps Job) | max delivery 3; concurrency guarded by SQL `sp_getapplock` per table (Basic SKU has no sessions) |
| `ingest-events` | topic | ingest | subscriptions: `stats`, `quality-alerts` | optional, Phase 5 |

SKU: **Basic** is enough for queues (cheapest). Topic `ingest-events` needs **Standard**; it is
optional and gated behind a Terraform flag `enable_ingest_topic` (default false). ADR-0003.

## 2. Envelope (all messages)

Service Bus properties: `message_id` = idempotency key, `correlation_id`, `content_type=application/json`,
application property `schema` = `<name>/v<major>`.

```jsonc
{ "schema": "backtest.job/v1", "message_id": "…", "correlation_id": "…",
  "produced_at": "2026-10-21T10:00:00Z", "producer": "tidal-api@1.4.0",
  "payload": { … } }
```

## 3. Payloads

### `backtest.job/v1`
```jsonc
{ "job_id": "bt_01J…", "parent_job_id": "bt_01J…" | null, "child_index": 3,
  "request": BacktestRequest_without_sweep,   // see api-contract.md, params resolved
  "output_prefix": "backtests/bt_01J…/child-0003/" }
```

### `backtest.result/v1`
```jsonc
{ "job_id": "…", "parent_job_id": "…", "status": "succeeded|failed",
  "metrics": {…} | null, "result_sha256": "…" | null,
  "error": { "code": "DATA_GAP", "detail": "…" } | null,
  "duration_ms": 8123, "node_pool": "spot", "attempt": 1 }
```

### `tiering.job/v1`
```jsonc
{ "table": "md.bar_hot", "hot_window_days": 90, "dry_run": false, "batch_rows": 500000 }
```

## 4. Consumer rules

- **Idempotent:** a worker that sees an existing `output_prefix/_SUCCESS` marker completes the
  message without recomputing.
- **No broker de-duplication:** Basic SKU has no duplicate detection, and `backtest-results` is
  delivered up to 5 times. The result aggregator is idempotent on `job_id`: a child already in a
  terminal state is not counted again (conditional `UPDATE ... WHERE status NOT IN (terminal)`).
- **Poison handling:** after max delivery → DLQ; `tidal dlq inspect` CLI lists and can resubmit.
- **Spot eviction:** SIGTERM handler abandons the message (not dead-letter) so it is redelivered.
- Messages > 200 KiB (204,800 bytes, serialised body) are not allowed; producers check before sending;
  large inputs go to Blob and the message carries a URL.
