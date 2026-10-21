# Config Contract — v1

Code mirror: `src/tidal/common/config.py` (pydantic-settings, prefix `TIDAL_`, nested delimiter `__`).
Every service reads only these keys. Unknown `TIDAL_*` keys fail startup (catches typos).

| Key | Type | Default | Secret? | Source in Azure |
|---|---|---|---|---|
| `TIDAL_ENV` | `local\|dev\|prod` | `local` | no | app setting |
| `TIDAL_LOG_LEVEL` | str | `INFO` | no | app setting |
| `TIDAL_STORAGE__ACCOUNT_URL` | url | Azurite URL | no | Terraform output |
| `TIDAL_STORAGE__USE_MANAGED_IDENTITY` | bool | `false` | no | `true` in Azure |
| `TIDAL_SQL__SERVER` | str | `localhost` | no | Terraform output |
| `TIDAL_SQL__DATABASE` | str | `tidal` | no | |
| `TIDAL_SQL__AUTH` | `entra_mi\|sql_password` | `sql_password` | no | `entra_mi` in Azure |
| `TIDAL_SQL__PASSWORD` | secret | — | **yes** | local only; never set in Azure |
| `TIDAL_SERVICEBUS__FQDN` | str | emulator | no | Terraform output |
| `TIDAL_AUTH__TENANT_ID` | uuid | — | no | |
| `TIDAL_AUTH__AUDIENCE` | str | `api://tidal` | no | |
| `TIDAL_AUTH__DISABLED` | bool | `false` | no | `true` only when `TIDAL_ENV=local` (startup asserts) |
| `TIDAL_SOURCES__EIA_API_KEY` | secret | — | **yes** | Key Vault reference |
| `TIDAL_SOURCES__FRED_API_KEY` | secret | — | **yes** | Key Vault reference |
| `TIDAL_HOT_WINDOW_DAYS` | int | `90` | no | |
| `TIDAL_SYNTH__SCALE` | `S\|M\|L` | `S` | no | |
| `TIDAL_SYNTH__SEED` | int | `20261021` | no | |
| `TIDAL_OTEL__CONNECTION_STRING` | secret | — | **yes** | Key Vault reference |

Rules:
- Secrets in Azure are **Key Vault references** (`@Microsoft.KeyVault(SecretUri=…)`) in App Service /
  Container Apps, and **CSI Secrets Store driver** (workload identity) on AKS.
- `Settings.__repr__` masks secret fields (`SecretStr`).
- Synthetic scales: `S` ≈ 50M ticks (3 instruments × 5 y), `M` ≈ 300M (10 × 10 y), `L` ≈ 1.5B (10 × 20 y).
