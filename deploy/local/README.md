# Local stack (docker compose)

Zero-cost stand-ins for Azure services (see `docs/LOCAL-DEV.md` §1). Compose file: `docker-compose.yml`
at the repo root.

| Service | Image (pinned) | Host ports | Stands in for |
|---|---|---|---|
| `azurite` | `azure-storage/azurite:3.37.0` | 10000 blob, 10001 queue, 10002 table | Blob Storage |
| `sqlserver` | `mssql/server:2022-CU27-ubuntu-22.04` (Developer edition) | 1433 | Azure SQL |
| `servicebus` | `azure-messaging/servicebus-emulator:2.0.1` | 5672 AMQP, 5300 mgmt/health | Service Bus |
| `api` (profile `app`) | built from `deploy/docker/Dockerfile`, target `dev` | 8000 | tidal-api |

`servicebus` reuses the `sqlserver` container as its metadata store (`SQL_SERVER=sqlserver`); there is
no second SQL instance. All ports bind to `127.0.0.1` only.

## Start

```bash
cp .env.example .env
# edit .env: set TIDAL_SQL__PASSWORD to your own strong password (see below)
docker compose up -d                  # infra only
docker compose --profile app up -d    # infra + api (needs the tidal-api entrypoint from T-110)
docker compose ps                     # wait for sqlserver / azurite to be "healthy"
docker compose down                   # stop; add -v to also drop the SQL volume
```

Azurite data lives in `./.data/azurite` (git-ignored); SQL data in the named volume `tidal_sqldata`.

## SQL `sa` password

The password comes from `TIDAL_SQL__PASSWORD` in your local `.env`, which is git-ignored and must never be
committed. Compose refuses to start without it. `.env.example` ships the placeholder `change-me-locally`,
which does **not** satisfy SQL Server's password policy (at least 8 characters, 3 of 4 classes: upper,
lower, digit, symbol), so choose your own value. The same variable is passed to the Service Bus emulator.
Changing it after the first start requires `docker compose down -v` because the password is baked into the
SQL data volume.

## Well-known connection strings (public dev values, not secrets)

Both strings are published by Microsoft in the Azurite and Service Bus emulator documentation and are
identical on every machine. They only work against local emulators. Because they look like credentials,
the `.env.example` line carries `# gitleaks:allow`.

- Azurite (account `devstoreaccount1`, host side):
  `DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;`
  Inside compose replace `127.0.0.1` with `azurite` (config contract: `http://azurite:10000/devstoreaccount1`).
- Service Bus emulator (host side):
  `Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;`
  Inside compose use `Endpoint=sb://servicebus;...`. `SAS_KEY_VALUE` is the literal static key.
  Management operations (admin client) need port `5300` appended to the endpoint; Tidal does not use them,
  entities come from `sb-config.json`.

## Service Bus entities (`sb-config.json`)

Source of truth: `docs/contracts/messaging-contract.md` §1. Config format documented at
<https://github.com/Azure/azure-service-bus-emulator-installer> (sample
`ServiceBus-Emulator/Config/Config.json`, schema `ServiceBus-Emulator/Schema/Config-schema.json`) and
<https://learn.microsoft.com/en-us/azure/service-bus-messaging/test-locally-with-service-bus-emulator>.
JSON cannot carry comments, so notes live here.

| Queue | Lock | Max delivery | DLQ on expiry | TTL |
|---|---|---|---|---|
| `backtest-jobs` | PT5M | 3 | yes | **PT1H (contract: 24 h)** |
| `backtest-results` | PT1M (default) | 5 | yes | default |
| `tiering-jobs` | PT1M (default) | 3 | default (off) | default |

Known deviation: the emulator schema caps `DefaultMessageTimeToLive` at PT1H (min PT5M), so the 24 h TTL of
the contract cannot be reproduced locally; the real TTL is set by Terraform. Max-delivery-driven dead-lettering
is always on in Service Bus. The emulator does not support duplicate detection (the contract does not need it).
Topic `ingest-events` is optional (Phase 5) and not defined.

## Database migrations

After `sqlserver` is healthy and `.env` is loaded:

```bash
uv run tidal db migrate
```

(The `db` command group arrives with the SQL repository task; it is not in `tidal.cli` yet.)

## Notes

- `docker compose config` validates the file without a daemon (needs `.env` for the required password).
- The `dev` image stage is not hardened (production stages: T-305).
