# SPEC-04 — Query API

Owner agent: `api-engineer` · Contracts: api-contract, openapi.yaml, config-contract
Tasks: T-110, T-111, T-209, T-210

## 1. Purpose
Secure, contract-exact HTTP API for traders, quants and admins. Two deployables from one codebase:
- **`tidal-api`** on **App Service** (Linux, container): auth, routing, backtest submission, small queries.
- **`tidal-query`** on **Container Apps** (scale 0→N on HTTP concurrency): heavy cold-store scans,
  called by `tidal-api` with an Entra token from `id-api` (audience `TIDAL_QUERY__AUDIENCE`, validated by
  `tidal-query`). Ingress: **external + token validation** while `enable_private_networking=false`
  (App Service cannot reach internal ingress without VNet integration); **internal** when the flag is on.
  Shows serverless containers next to a classic PaaS host.

## 2. Structure
```
api/app.py            create_app(settings) factory; lifespan opens clients
api/deps.py           get_reader, get_bus, get_principal (DI → fakes in tests)
api/auth.py           Entra ID JWT validation (JWKS cached 1 h), role check decorator
api/routers/{health,instruments,bars,ticks,latest,spreads,backtests,admin}.py
api/errors.py         TidalError → problem+json
api/streaming.py      arrow / parquet streaming responses
```

## 3. Rules
- Phase 1: routers built against `FakeReader`/`FakeBus` implementing the same protocols as real ones.
- Validation: max range per call (`ticks` ≤ 31 d; `bars 1s` ≤ 7 d; `1d` unlimited) → `413` otherwise.
- `fill=ffill` only in API layer; storage never forward-fills (business-knowledge §3).
- Rate limit: 60 req/min per principal (in-memory token bucket; documented limitation for multi-instance).
- OpenTelemetry: one span per request + child spans for SQL / Blob / Service Bus; `tier` as span attribute.
- Backtest submit: validate, expand `sweep` into children (cartesian product, max 200), write parent row
  in `ops.backtest_job`, send messages in **batches** (`ServiceBusMessageBatch`), return `202`.

## 4. Acceptance criteria
- [ ] `oasdiff breaking` reports nothing in **either** direction (contract→generated, generated→contract);
      cosmetic differences of FastAPI's 3.1 output (e.g. `anyOf` vs type arrays) are allowed.
- [ ] Contract tests: schemathesis run against the app with fakes, 0 failures.
- [ ] Auth tests: missing token 401, wrong audience 401, missing role 403, correct role 200, role
      hierarchy per api-contract §1.1 (tokens minted by the local test JWKS from `scripts/dev-token.py`).
- [ ] `/bars` for 20 yrs daily Brent returns < 300 ms p95 (warm) on App Service B1.
- [ ] `format=arrow` streams 10M rows with RSS < 500 MB.
- [ ] Sweep of 6×5 params creates 30 messages and one parent job.
