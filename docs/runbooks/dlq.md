# Runbook: dead-letter queues (T-602)

Purpose: find out why a Service Bus message was dead-lettered, then replay it or discard it. Covers the queues
`backtest-jobs`, `backtest-results` and `tiering-jobs` (`docs/contracts/messaging-contract.md` sections 1 and 4).

Steps that need a deployed environment are marked **[deployed]**. Everything else can be tried locally against the
Service Bus emulator (`docker compose up -d`) with `TIDAL_SERVICEBUS__CONNECTION_STRING` set.

## How a message gets there
| Cause | Reason string on the message | Who does it | Retry helps? |
|---|---|---|---|
| Body over 204,800 bytes | `message-too-large` | worker (`worker/consumer.py`) | no |
| Body is not JSON | `invalid-json` | worker | no |
| `schema` field is not the one this queue's handler expects | `unknown-schema` | worker | no, unless the message went to the wrong queue |
| Envelope fails pydantic validation | `invalid-message` (description lists `field: error-type`, never the value) | worker | no |
| Job raised on the last allowed attempt | `max-delivery-exceeded` (description is the exception class name) | worker | maybe: depends on the cause |
| Delivery count reached the queue limit before the worker's own check | `MaxDeliveryCountExceeded` | broker | maybe |
| Message outlived its TTL | `TTLExpiredException` | broker (`dead_lettering_on_message_expiration = true`) | only if the job is still wanted |

Limits come from `infra/terraform/modules/servicebus/variables.tf` and match the contract:

| Queue | Max delivery | Lock | TTL |
|---|---|---|---|
| `backtest-jobs` | 3 | 5 min | 1 day |
| `backtest-results` | 5 | Service Bus default (1 min) | Basic-tier maximum (14 days) |
| `tiering-jobs` | 3 | Service Bus default (1 min) | 1 day |

Things worth knowing:
- The worker counts attempts as `delivery_count + 1` and dead-letters itself on the third failed attempt
  (`MAX_DELIVERY = 3` in `worker/main.py`). Before that it abandons, so the broker redelivers.
- A SIGTERM (spot eviction) also abandons the message. Abandons raise the broker's delivery count, so a job that is
  evicted repeatedly can reach `MaxDeliveryCountExceeded` without ever failing on its own.
- On the last attempt the handler's `on_exhausted` runs first. For `backtest-jobs` it sends a `failed`
  `backtest.result/v1` to `backtest-results`, so the API sees the failure even though the job message is in the DLQ.
- `backtest-results` has no worker in this repo. Its consumer is the API result aggregator named in the contract.

## Which alert fires
`alert-tidal-dlq-<env>` (`infra/terraform/modules/monitoring/main.tf`): metric alert on the namespace metric
`DeadletteredMessages`, maximum over 5 minutes, threshold greater than 0, split by `EntityName`, severity 2. It
notifies the single action group of the budget module and auto-resolves when the DLQ is empty. The alert description
points to `tidal dlq inspect`.

## Prerequisites **[deployed]**
- `az login`, and Entra data roles on the queue you work on: `Azure Service Bus Data Receiver` to inspect,
  plus `Azure Service Bus Data Sender` to resubmit (the namespace has SAS keys off: `local_auth_enabled = false`).
  The expected assignments are in `infra/terraform/envs/dev/rbac.tf`; do not add roles by hand.
- Environment variables for the CLI (no secrets; values from `terraform output -json tidal_config` in
  `infra/terraform/envs/dev`): `TIDAL_SERVICEBUS__FQDN` (`sb-tidal-dev-<suffix>.servicebus.windows.net`).
  The CLI uses `DefaultAzureCredential` and AMQP over WebSocket (port 443) outside `local`.

## 1. Inspect (read-only)
Peek does not lock or change anything:
```bash
uv run tidal dlq inspect backtest-jobs --max 50
uv run tidal dlq inspect tiering-jobs
uv run tidal dlq inspect backtest-results
```
Each line: message id, schema, `reason=`, `deliveries=`, `bytes=`, description. Bodies are not printed.

Count per queue without the CLI **[deployed]**:
```bash
az servicebus queue show -g <rg-name> --namespace-name <sb-namespace> -n backtest-jobs \
  --query countDetails.deadLetterMessageCount
```
`<rg-name>` is output `resource_group_name`; the namespace is `sb-tidal-dev-<suffix>`.

Service Bus Explorer in the portal is the alternative. Use the peek function only; "receive" there deletes.

Then find the cause in logs. Every worker log line carries the message's `correlation_id`; search Log Analytics
(workbook or Logs blade) for it, and for the events `dead_letter`, `max_delivery_exceeded`, `job_failed_retry`.

## 2. Decide: replay or discard
| Finding | Action |
|---|---|
| `invalid-json`, `invalid-message`, `unknown-schema`, `message-too-large` | Discard. The producer is wrong; fix the producer (API) first. Replaying returns it to the DLQ. |
| `max-delivery-exceeded`, cause was transient (Blob, SQL or Service Bus outage, spot eviction burst, deploy in progress) and fixed | Replay. |
| `max-delivery-exceeded`, cause is a bug or bad input (`DATA_GAP`, code error) | Do not replay yet. Fix, deploy, then replay. For `backtest-jobs` the user already received a `failed` result. |
| `TTLExpiredException` on `backtest-jobs` | Usually discard: the user's request is a day old. Resubmit the backtest through the API if still wanted. |
| `TTLExpiredException` on `tiering-jobs` | Replay, or run `tidal tiering run` by hand. Both do the same work. |
| Unsure | Leave it. Messages in the DLQ do not expire and cost nothing at this volume. |

Idempotency, so replay is safe:
- Backtest jobs: a job whose `output_prefix/_SUCCESS` marker exists is completed without recomputing.
- `backtest-results`: the aggregator ignores a child already in a terminal state (conditional `UPDATE`).
  Basic SKU has no broker duplicate detection, so duplicates are possible and expected.
- Tiering jobs: tiering is idempotent and resumable; a held SQL applock is a normal completion (`locked`), not a failure.
  Replaying twice is harmless.
- `resubmit` copies first, then completes the DLQ message. A crash in between duplicates the message, never loses it.

## 3. Replay **[deployed]**
Preview with step 1, then one message first:
```bash
uv run tidal dlq resubmit backtest-jobs --message-id <message-id>
uv run tidal dlq resubmit tiering-jobs --max 50
```
The command prints `resubmitted <n>`. The copy keeps the original message id, correlation id and `schema` property. It
goes back through the normal path, so the next attempt counts from zero.

Verification:
- `tidal dlq inspect <queue>` no longer lists it, and the `alert-tidal-dlq-<env>` alert resolves within about 10 minutes.
- `backtest-jobs`: the KEDA ScaledJob starts a worker pod; a `backtest.result/v1` with `status=succeeded` arrives.
- `tiering-jobs`: the worker log shows `status` `done` (or `locked`).

If the message is dead-lettered again with the same reason, stop and fix the cause. Do not loop.

## 4. Discard
There is no discard command in the CLI on purpose. After you have recorded the message id, reason and cause (PR, issue or
the incident note), remove it in Service Bus Explorer using "receive and delete" on the dead-letter sub-queue, one message
at a time, or let it stay. Needs the `Azure Service Bus Data Receiver` role.

## Rollback
Replay is a copy, so there is nothing to roll back except its effect: a replayed backtest or tiering job is idempotent.
If a replay makes things worse, stop the consumer (`make park` stops AKS and the jobs, see `docs/runbooks/park.md`)
and inspect again.

## Related
- `src/tidal/worker/dlq.py`, `src/tidal/worker/consumer.py`
- `docs/runbooks/restore.md` for re-running tiering
