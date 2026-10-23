# ADR-0016: The AKS backtest worker reads market data through tidal-query

- Status: Accepted
- Date: 2026-10-24
- Deciders: Müge Güçlü (+ orchestrator, platform-engineer)
- Related: SPEC-06 §2, SPEC-07 §5, T-303, T-307, T-402, T-403

## Context
The Helm chart (T-307) puts the worker namespace behind a default-deny NetworkPolicy that only allows
egress on TCP 443 and DNS. SPEC-06 §2 originally gave `id-worker` `db_datareader` on `md` and `ref` so the
unified reader could read the hot window from Azure SQL. SQL needs port 1433 (or the 11000-11999 redirect
range), which the policy blocks, and opening it would widen the pod network surface.

## Decision
The worker reads bars through `tidal-query` over HTTPS (443) with `RemoteReader`, authenticating with an
Entra token for the query audience (`TIDAL_QUERY__AUDIENCE`, app role defined in T-402). It has no SQL
access: no `db_datareader`, no SQL firewall rule for the AKS egress.

## Why
- Egress is 443 only; SQL needs 1433 or redirect ports.
- One data path: hot/cold routing, as-of semantics and caching live in one service.
- Least privilege: a compromised backtest pod cannot read the database directly.

## Consequences
- Query load and latency of `tidal-query` now bound backtest read throughput; scale rules for the query
  app (max 5) matter for a 10-pod fan-out. Measure in T-308/T-313.
- `Settings` validation still demands `TIDAL_SQL__AUTH=entra_mi` and a real `TIDAL_SQL__SERVER` outside
  local, so the chart sets them for config validation only; no SQL connection is opened.
- Service Bus must use AMQP over WebSockets (443).
- KEDA identity: the scaler authenticates as the KEDA operator identity (`keda.identityClientId`, federated
  to `system:serviceaccount:kube-system:keda-operator`), not as `id-worker`. That identity holds Service Bus
  Data Owner on `backtest-jobs` (T-403); the worker keeps only Receiver on `backtest-jobs` and Sender on
  `backtest-results`.
