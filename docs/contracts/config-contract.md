# Config Contract — v1

Code mirror: `src/tidal/common/config.py` (pydantic-settings, prefix `TIDAL_`, nested delimiter `__`).
Every service reads only these keys. Unknown `TIDAL_*` keys fail startup (catches typos).

| Key | Type | Default | Secret? | Source in Azure |
|---|---|---|---|---|
| `TIDAL_ENV` | `local\|dev\|prod` | `local` | no | app setting |
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
| `TIDAL_SOURCES__EIA_API_KEY` | secret | — | **yes** | Key Vault reference |
| `TIDAL_SOURCES__FRED_API_KEY` | secret | — | **yes** | Key Vault reference |
| `TIDAL_HOT_WINDOW_DAYS` | int | `90` | no | must be > 0 |
| `TIDAL_SYNTH__SCALE` | `S\|M\|L` | `S` | no | |
| `TIDAL_SYNTH__SEED` | int | `20261021` | no | |
| `TIDAL_SYNTH__START` | date | `2005-01-01` | no | first synthetic day (UTC) |
| `TIDAL_SYNTH__END` | date | `2025-12-31` | no | last synthetic day (≥ `SYNTH__START`); part of the determinism key |
| `TIDAL_CLOCK__NOW` | datetime | — | no | local/test only: freezes "now" for the router and tiering (hot-window demos on synthetic data) |
| `TIDAL_OTEL__CONNECTION_STRING` | secret | — | **yes** | Key Vault reference |

Rules:
- `AZURE_CLIENT_ID` (user-assigned managed identity) is read by `DefaultAzureCredential`, not by
  Tidal settings; it is outside the `TIDAL_` namespace and therefore not subject to the unknown-key rule.
- `TIDAL_STORAGE__CONNECTION_STRING`, `TIDAL_SERVICEBUS__CONNECTION_STRING`, `TIDAL_SQL__PASSWORD` and
  `TIDAL_CLOCK__NOW` set while `TIDAL_ENV != local` fail startup. `TIDAL_OTEL__CONNECTION_STRING` is exempt:
  in Azure it is a Key Vault reference.
- `TIDAL_SQL__AUTH=entra_mi` requires a real server (not empty, `localhost` or `127.0.0.1`).
- `principal_oid` is logged as unsalted sha256 (SPEC-10 §1); a keyed HMAC is reviewed in the T-406 threat model.
- "Now" comes from an injected clock (`tidal.common.timeutil.Clock`) everywhere; only the clock reads
  wall time or `TIDAL_CLOCK__NOW`.
- Secrets in Azure are **Key Vault references** (`@Microsoft.KeyVault(SecretUri=…)`) in App Service /
  Container Apps, and **CSI Secrets Store driver** (workload identity) on AKS.
- `Settings.__repr__` masks secret fields (`SecretStr`).
- Synthetic scales: `S` ≈ 50M ticks (3 instruments × 5 y), `M` ≈ 300M (10 × 10 y), `L` ≈ 1.5B (10 × 20 y).
