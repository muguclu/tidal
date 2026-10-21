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

Stored in Azure SQL `ref.instrument` and as `reference/instruments.parquet` in Blob.

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
| `volume` | float64 | ≥ 0, NaN allowed for sources without volume (EIA spot) |
| `trade_count` | int32 | nullable |
| `quality` | int8 | bit flags |
| `ingested_at_ns` | int64 | |
| `version` | int16 | |

Daily EIA/FRED values are stored as `1d` bars with `open=high=low=close=value`, `volume=NaN`.

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
CREATE TABLE md.bar_hot (
  instrument_key SMALLINT  NOT NULL,      -- FK ref.instrument.instrument_key
  interval_code  TINYINT   NOT NULL,      -- 1s=1,1m=2,1h=3,1d=4
  ts_ns          BIGINT    NOT NULL,
  [open] FLOAT NOT NULL, high FLOAT NOT NULL, low FLOAT NOT NULL, [close] FLOAT NOT NULL,
  volume FLOAT NULL, trade_count INT NULL, quality TINYINT NOT NULL,
  ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL,
  CONSTRAINT pk_bar_hot PRIMARY KEY CLUSTERED (instrument_key, interval_code, ts_ns, version)
) WITH (DATA_COMPRESSION = PAGE);

CREATE TABLE md.tick_hot ( ... same pattern as TICK_V1 ... ) WITH (DATA_COMPRESSION = PAGE);
CREATE TABLE ops.migration_watermark (table_name SYSNAME PRIMARY KEY, migrated_until_ns BIGINT NOT NULL, updated_at DATETIME2 NOT NULL);
```

The **baseline** for benchmarks is the same tables *without* compression and with `DATETIME2`
instead of `ts_ns` (`md.bar_baseline`), to show the epoch + compression gain honestly.

## 7. Versioning rules

- Additive nullable column → minor (`v1.1`), no ADR needed, update this doc + tests.
- Rename/remove/type change → major (`v2`), ADR required, both versions readable for one phase.
