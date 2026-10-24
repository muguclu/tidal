# Config Contract — v1.3

Code mirror: `src/tidal/common/config.py` (pydantic-settings, prefix `TIDAL_`, nested delimiter `__`).
Every service reads only these keys. Unknown `TIDAL_*` keys fail startup (catches typos).

| Key | Type | Default | Secret? | Source in Azure |
|---|---|---|---|---|
| `TIDAL_ENV` | `local\|dev\|prod` | **required** | no | app setting; docker compose and `.env.example` set `local` |
| `TIDAL_LOG_LEVEL` | str | `INFO` | no | app setting |
| `TIDAL_SERVICE_NAME` | `api\|query\|worker\|ingest\|tiering\|legacy` | `api` | no | set per entrypoint/deployment |
| `TIDAL_STORAGE__ACCOUNT_URL` | url | `http://127.0.0.1:10000/devstoreaccount1` | no | Terraform output; docker compose sets `http://azurite:10000/devstoreaccount1` |
| `TIDAL_STORAGE__USE_MANAGED_IDENTITY` | bool | `false` | no | `true` in Azure |
| `TIDAL_STORAGE__CONNECTION_STRING` | secret | — | **yes** | local only (Azurite well-known key); never set in Azure |
| `TIDAL_SQL__SERVER` | str | `localhost` | no | Terraform output |
| `TIDAL_SQL__DATABASE` | str | `tidal` | no | |
| `TIDAL_SQL__AUTH` | `entra_mi\|sql_password` | `sql_password` | no | `entra_mi` in Azure |
| `TIDAL_SQL__USER` | str | `sa` | no | local only |
| `TIDAL_SQL__PASSWORD` | secret | — | **yes** | local only; never set in Azure |
| `TIDAL_SERVICEBUS__FQDN` | str | `localhost` | no | Terraform output |
| `TIDAL_SERVICEBUS__CONNECTION_STRING` | secret | — | **yes** | local only (emulator, `UseDevelopmentEmulator=true`); never set in Azure |
| `TIDAL_QUERY__BASE_URL` | url | `http://query:8000` | no | Container Apps FQDN (Terraform output) |
| `TIDAL_QUERY__AUDIENCE` | str | `api://tidal-query` | no | Entra audience `tidal-api` requests a token for |
| `TIDAL_AUTH__TENANT_ID` | uuid | — | no | required when auth is enabled and `TIDAL_ENV != local` |
| `TIDAL_AUTH__AUDIENCE` | str | `api://tidal` | no | |
| `TIDAL_AUTH__DISABLED` | bool | `false` | no | `true` only when `TIDAL_ENV=local` (startup asserts) |
| `TIDAL_AUTH__JWKS_URL` | str | — | no | local only: http(s) URL or file path (e.g. `.data/dev-jwks/jwks.json`); default is the tenant's Entra v2.0 keys endpoint; set outside local fails startup |
| `TIDAL_AUTH__QUERY_ROLE` | str | `Tidal.Query.Read` | no | app role `tidal-query` requires in the caller's token |
| `TIDAL_AUTH__API_APP_CLIENT_ID` | str | — | no | tidal-api app registration client id GUID; accepted as `aud` next to `TIDAL_AUTH__AUDIENCE` (v2 tokens carry the GUID) |
| `TIDAL_QUERY__APP_CLIENT_ID` | str | — | no | tidal-query app registration client id GUID; accepted as `aud` next to `TIDAL_QUERY__AUDIENCE` |
| `TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS` | str | — | no | comma-separated GUIDs (`id-api`, `id-worker`); tidal-query requires `azp`/`appid` in this list OR `oid` in the object-id list, plus the query role |
| `TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS` | str | — | no | comma-separated principal object id GUIDs; both lists empty fails `tidal-query` startup outside local |
| `TIDAL_CURSOR__KEY` | secret | — | **yes** | local only (>= 32 bytes); unset locally gives a per-process random key; never set in Azure |
| `TIDAL_CURSOR__KEYVAULT_URL` | url | — | no | required outside local: Key Vault read with the app's Managed Identity |
| `TIDAL_CURSOR__SECRET_NAME` | str | `cursor-hmac-key` | no | Key Vault secret holding the HMAC key (>= 32 bytes) |
| `TIDAL_HOT_WINDOW_DAYS` | int | `90` | no | must be > 0 |
| `TIDAL_SYNTH__SCALE` | `S\|M\|L` | `S` | no | |
| `TIDAL_SYNTH__SEED` | int | `20261021` | no | |
| `TIDAL_SYNTH__START` | date | `2005-01-01` | no | first synthetic day (UTC) |
| `TIDAL_SYNTH__END` | date | `2025-12-31` | no | last synthetic day (≥ `SYNTH__START`); part of the determinism key |
| `TIDAL_CLOCK__NOW` | datetime | — | no | local/test only: freezes "now" for the router and tiering (hot-window demos on synthetic data) |
| `TIDAL_OTEL__CONNECTION_STRING` | secret | — | **yes** | Key Vault reference |

Rules:
- `TIDAL_ENV` has no default: unset fails startup with a `ConfigError` naming `TIDAL_ENV` (fail closed).
  Every entrypoint, script and test that builds settings passes it explicitly (`local` for dev tooling).
- `AZURE_CLIENT_ID` (user-assigned managed identity) is read by `DefaultAzureCredential`, not by
  Tidal settings; it is outside the `TIDAL_` namespace and therefore not subject to the unknown-key rule.
- `TIDAL_STORAGE__CONNECTION_STRING`, `TIDAL_SERVICEBUS__CONNECTION_STRING`, `TIDAL_SQL__PASSWORD` and
  `TIDAL_CLOCK__NOW`, `TIDAL_AUTH__JWKS_URL` and `TIDAL_CURSOR__KEY` set while `TIDAL_ENV != local` fail startup. `TIDAL_OTEL__CONNECTION_STRING` is exempt:
  in Azure it is a Key Vault reference.
- Outside local, `TIDAL_SQL__AUTH` must be `entra_mi` and `TIDAL_STORAGE__USE_MANAGED_IDENTITY` must be `true` (SPEC-06: Entra-only).
- `TIDAL_SQL__AUTH=entra_mi` requires a real server (not empty, `localhost` or `127.0.0.1`).
- `principal_oid` is logged as unsalted sha256 (SPEC-10 §1); a keyed HMAC is reviewed in the T-406 threat model.
- "Now" comes from an injected clock (`tidal.common.timeutil.Clock`) everywhere; only the clock reads
  wall time or `TIDAL_CLOCK__NOW`.
- Secrets in Azure are **Key Vault references** (`@Microsoft.KeyVault(SecretUri=…)`) in App Service /
  Container Apps, and **CSI Secrets Store driver** (workload identity) on AKS.
- `Settings.__repr__` masks secret fields (`SecretStr`).
- Synthetic scales: `S` ≈ 50M ticks (3 instruments × 5 y), `M` ≈ 300M (10 × 10 y), `L` ≈ 1.5B (10 × 20 y).

## Changelog

- **v1.1 (T-114)**: `TIDAL_ENV` is required (was default `local`). Reason: the T-112 security review
  found that an Azure deployment which forgot `TIDAL_ENV` would silently get local rules
  (connection strings, SQL password and `TIDAL_CLOCK__NOW` allowed). Fail closed instead. Minor
  bump: no key, type or allowed value changes; docker compose and `.env.example` already set it,
  scripts and tests now pass it explicitly, and no Azure app settings exist yet that could omit it.
- **v1.2 (T-117, ADR-0013)**: removed `TIDAL_SOURCES__EIA_API_KEY` and `TIDAL_SOURCES__FRED_API_KEY`
  (the `sources` group). All data is synthetic; no upstream vendor keys exist. Because unknown
  `TIDAL_*` keys fail startup, a deployment that still sets either variable now stops with a
  `ConfigError` naming the key. Minor bump by the same reasoning as v1.1: no deployed consumer
  (no Azure app settings or Key Vault secrets for these keys were ever created).
- **v1.3 (T-402)**: added `TIDAL_AUTH__JWKS_URL` (local only), `TIDAL_AUTH__QUERY_ROLE`,
  `TIDAL_AUTH__API_APP_CLIENT_ID`, `TIDAL_QUERY__APP_CLIENT_ID` (v2 tokens carry the app client id
  GUID as `aud`; both forms are accepted), `TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS`,
  `TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS` (tidal-query caller allowlists, ADR-0016: id-api and
  id-worker) and the `TIDAL_CURSOR__*` group (HMAC key
  from Key Vault so cursors survive restarts and replicas). Minor bump: additive keys with
  defaults. Outside local `tidal-api` requires `TIDAL_CURSOR__KEYVAULT_URL` at startup (app
  factory, not `Settings`), and `TIDAL_AUTH__JWKS_URL` / `TIDAL_CURSOR__KEY` fail startup.
  T-401 supplies the client/object ids and the Key Vault URL as app settings.
