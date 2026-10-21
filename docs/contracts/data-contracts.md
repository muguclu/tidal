# Data Contracts — v1

Status: **DRAFT → frozen at tag `contracts-v1` (end of Phase 0)**
Code mirror: `src/tidal/contracts/schemas.py` (pyarrow schemas) and `models.py` (pydantic).
Contract tests: `tests/contract/test_schemas.py` asserts code == this document.

## 1. Instrument (reference data)

| Field | Type | Null | Rule |
|---|---|---|---|
| `instrument_id` | string | no | `^[A-Z0-9]+\.[A-Z0-9_]+\.[A-Z0-9_]+$`, e.g. `EIA.BRENT.SPOT`, `SYN.POWER_DE.TICK` |
| `source` | enum | no | `EIA`, `FRED`, `SYN` |
| `asset_class` | enum | no | `crude`, `gas`, `power`, `fx`, `refined` |
| `kind` | enum | no | `spot`, `future`, `continuous`, `fx` |
| `unit` | string | no | `USD/bbl`, `USD/MMBtu`, `EUR/MWh`, `USD/EUR` … |
| `currency` | string(3) | no | ISO 4217 |
| `exchange_tz` | string | no | IANA tz, e.g. `America/New_York` |
| `granularity` | enum | no | `tick`, `1s`, `1m`, `1h`, `1d` (native granularity) |
| `roll_method` | enum | yes | `ratio`, `difference`, `none`; required when `kind=continuous` |
| `first_ts_ns` / `last_ts_ns` | int64 | yes | maintained by ingest |
| `description` | string | no | human readable |

Stored in Azure SQL `ref.instrument` (with a surrogate `instrument_key SMALLINT`, see §6) and as
`reference/instruments.parquet` in Blob.

### 1.1 Instrument catalogue v1 (normative seed)

| instrument_id | source series | asset_class | kind | unit | ccy | exchange_tz | granularity |
|---|---|---|---|---|---|---|---|
| `EIA.BRENT.SPOT` | EIA `RBRTE` | crude | spot | USD/bbl | USD | Europe/London | 1d |
| `EIA.WTI.SPOT` | EIA `RWTC` | crude | spot | USD/bbl | USD | America/Chicago | 1d |
| `EIA.WTI.FUT1` … `EIA.WTI.FUT4` | EIA `RCLC1`…`RCLC4` | crude | future | USD/bbl | USD | America/New_York | 1d |
| `EIA.HH.SPOT` | EIA `RNGWHHD` | gas | spot | USD/MMBtu | USD | America/Chicago | 1d |
| `EIA.HH.FUT1` … `EIA.HH.FUT4` | EIA `RNGC1`…`RNGC4` | gas | future | USD/MMBtu | USD | America/New_York | 1d |
| `FRED.EURUSD.FX` | FRED `DEXUSEU` | fx | fx | USD/EUR | USD | America/New_York | 1d |
| `SYN.BRENT.TICK`, `SYN.WTI.TICK`, `SYN.HH.TICK`, `SYN.NBP.TICK`, `SYN.TTF.TICK`, `SYN.POWER_DE.TICK`, `SYN.POWER_FR.TICK`, `SYN.GASOIL.TICK`, `SYN.RBOB.TICK`, `SYN.EURUSD.TICK` | generator | per name | spot | per name | per name | per name | tick |

`FUTn` = EIA "contract n" series (n-th nearby, no roll adjustment → `kind=future`, `roll_method=null`).
They make M1–M2 calendar spreads possible from free data. S scale uses the first three `SYN.*` rows.
EIA series IDs are verified by T-102 against the live API; a mismatch is a CHANGE-REQUESTS entry.

## 2. Tick (arrow schema `TICK_V1`)

| Column | Arrow type | Encoding (Parquet) | Rule |
|---|---|---|---|
| `ts_ns` | int64 | DELTA_BINARY_PACKED | UTC epoch nanoseconds; **sorted ascending within file** |
| `instrument_id` | dictionary<int16, string> | RLE_DICTIONARY | one value per file in cold storage |
| `price` | float64 | BYTE_STREAM_SPLIT | may be negative; NaN not allowed |
| `size` | float32 | BYTE_STREAM_SPLIT | ≥ 0; 0 for quotes |
| `side` | int8 | RLE | 0=trade, 1=bid, 2=ask |
| `quality` | int8 | RLE | bit flags, see §5 |
| `ingested_at_ns` | int64 | DELTA_BINARY_PACKED | knowledge time |
| `version` | int16 | RLE | 1 for first write, +1 per restatement |

Primary key: (`instrument_id`, `ts_ns`, `side`, `version`).

## 3. Bar (arrow schema `BAR_V1`)

| Column | Arrow type | Rule |
|---|---|---|
| `ts_ns` | int64 | bar **open** time, UTC epoch ns |
| `instrument_id` | dictionary<int16,string> | |
| `interval` | dictionary<int8,string> | `1s`,`1m`,`1h`,`1d` |
| `open`,`high`,`low`,`close` | float64 | `low ≤ min(open,close)`, `high ≥ max(open,close)` |
| `volume` | float64 | ≥ 0, NaN allowed for sources without volume (EIA spot) — see §3.1 |
| `trade_count` | int32 | nullable |
| `quality` | int8 | bit flags |
| `ingested_at_ns` | int64 | |
| `version` | int16 | |

Daily EIA/FRED values are stored as `1d` bars with `open=high=low=close=value`, `volume=NaN`.

### 3.1 Missing-value mapping (normative)
SQL Server `FLOAT` cannot store NaN. "No value" is therefore represented as:
Parquet/Arrow → `NaN` · SQL → `NULL` · API JSON → `null`. Conversion happens only in
`storage.sql_repo` (NaN↔NULL) and at the API edge (NaN→null). `price`, `open..close` are never NaN.

### 3.2 Derived bars
`bars_1s`, `bars_1m`, `bars_1h` for `SYN.*` instruments are aggregated from ticks (trades only,
`side=0`) by `storage.aggregate` (T-107): `open`=first, `high`=max, `low`=min, `close`=last,
`volume`=Σsize, `trade_count`=count, `quality`=OR of tick flags; empty intervals produce no bar.

## 4. Backtest result (arrow schema `BT_EQUITY_V1`, `BT_TRADES_V1`)

`BT_EQUITY_V1`: `ts_ns int64, equity float64, position float64, drawdown float64`
`BT_TRADES_V1`: `ts_ns int64, instrument_id string, qty float64, price float64, fee float64, slippage float64`
Metrics JSON: see `api-contract.md#BacktestResult`.

## 5. Quality flags (int8 bitmask)

| Bit | Name | Meaning |
|---|---|---|
| 0 | `NEGATIVE` | price < 0 (valid, flagged) |
| 1 | `OUTLIER` | |Δ| > 8σ of rolling 1000-obs window |
| 2 | `GAP_BEFORE` | expected observation(s) missing before this one |
| 3 | `RESTATED` | version > 1 |
| 4 | `SYNTHETIC` | produced by generator |
| 5–7 | reserved | must be 0 |

## 6. Hot-store SQL tables (Azure SQL)

```sql
CREATE SCHEMA ref; CREATE SCHEMA md; CREATE SCHEMA ops;

CREATE TABLE ref.instrument (
  instrument_key SMALLINT IDENTITY(1,1) PRIMARY KEY,
  instrument_id  VARCHAR(64) NOT NULL UNIQUE,
  source VARCHAR(8) NOT NULL, asset_class VARCHAR(16) NOT NULL, kind VARCHAR(16) NOT NULL,
  unit VARCHAR(16) NOT NULL, currency CHAR(3) NOT NULL, exchange_tz VARCHAR(64) NOT NULL,
  granularity VARCHAR(4) NOT NULL, roll_method VARCHAR(16) NULL,
  first_ts_ns BIGINT NULL, last_ts_ns BIGINT NULL, description NVARCHAR(256) NOT NULL
);

CREATE TABLE md.bar_hot (
  instrument_key SMALLINT  NOT NULL,      -- FK ref.instrument.instrument_key
  interval_code  TINYINT   NOT NULL,      -- 1s=1,1m=2,1h=3,1d=4
  ts_ns          BIGINT    NOT NULL,
  [open] FLOAT NOT NULL, high FLOAT NOT NULL, low FLOAT NOT NULL, [close] FLOAT NOT NULL,
  volume FLOAT NULL, trade_count INT NULL, quality TINYINT NOT NULL,
  ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL,
  CONSTRAINT pk_bar_hot PRIMARY KEY CLUSTERED (instrument_key, interval_code, ts_ns, version)
) WITH (DATA_COMPRESSION = PAGE);

CREATE TABLE md.tick_hot (
  instrument_key SMALLINT NOT NULL,
  ts_ns          BIGINT   NOT NULL,
  price FLOAT NOT NULL, size REAL NOT NULL, side TINYINT NOT NULL, quality TINYINT NOT NULL,
  ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL,
  CONSTRAINT pk_tick_hot PRIMARY KEY CLUSTERED (instrument_key, ts_ns, side, version)
) WITH (DATA_COMPRESSION = PAGE);

CREATE TABLE ops.migration_watermark (
  table_name SYSNAME PRIMARY KEY, migrated_until_ns BIGINT NOT NULL, updated_at DATETIME2(3) NOT NULL);

CREATE TABLE ops.ingest_checkpoint (
  source VARCHAR(8) NOT NULL, instrument_id VARCHAR(64) NOT NULL, last_ts_ns BIGINT NOT NULL,
  updated_at DATETIME2(3) NOT NULL, CONSTRAINT pk_ingest_checkpoint PRIMARY KEY (source, instrument_id));

CREATE TABLE ops.backtest_job (
  job_id CHAR(29) PRIMARY KEY,                 -- bt_ + ULID
  parent_job_id CHAR(29) NULL, child_index INT NULL,
  principal_oid_hash CHAR(64) NOT NULL,        -- sha256 of Entra oid, never the raw oid
  status VARCHAR(16) NOT NULL,                 -- queued|running|succeeded|failed|partial
  request_json NVARCHAR(MAX) NOT NULL, metrics_json NVARCHAR(MAX) NULL,
  result_sha256 CHAR(64) NULL, error_code VARCHAR(32) NULL,
  children_total INT NULL, children_succeeded INT NULL, children_failed INT NULL,
  submitted_at DATETIME2(3) NOT NULL, finished_at DATETIME2(3) NULL
);
CREATE INDEX ix_backtest_job_parent ON ops.backtest_job(parent_job_id);
```

SQL `ts_ns` columns hold the same int64 as Parquet. Migrations live in `sql/migrations/` (T-106) and
are applied by `tidal db migrate`.

The **baseline** for benchmarks is the same tables *without* compression and with `DATETIME2`
instead of `ts_ns` (`md.bar_baseline`), to show the epoch + compression gain honestly.

## 7. Restatements and as-of (normative)

- Every restatement appends a row with `version+1`, a later `ingested_at_ns` and the `RESTATED` flag.
  **No version is ever deleted** — not by migration, not by compaction — because `as_of` needs them.
- Latest view: per key, the row with max `version`.
- As-of view (`as_of = T`): per key, the row with max `version` among rows with `ingested_at_ns ≤ T`;
  keys with no such row are absent.

## 8. Versioning rules

- Additive nullable column → minor (`v1.1`), no ADR needed, update this doc + tests.
- Rename/remove/type change → major (`v2`), ADR required, both versions readable for one phase.
