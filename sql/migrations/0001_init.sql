-- 0001_init: hot-store DDL, identical to docs/contracts/data-contracts.md section 6, plus the
-- md.bar_baseline benchmark table. Idempotency comes from the runner (ops.schema_version).
IF SCHEMA_ID('ref') IS NULL EXEC('CREATE SCHEMA ref');
IF SCHEMA_ID('md') IS NULL EXEC('CREATE SCHEMA md');
IF SCHEMA_ID('ops') IS NULL EXEC('CREATE SCHEMA ops');

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

-- Benchmark baseline (data-contracts section 6): same as md.bar_hot but DATETIME2 instead of
-- ts_ns and no compression.
CREATE TABLE md.bar_baseline (
  instrument_key SMALLINT  NOT NULL,
  interval_code  TINYINT   NOT NULL,
  ts             DATETIME2(7) NOT NULL,
  [open] FLOAT NOT NULL, high FLOAT NOT NULL, low FLOAT NOT NULL, [close] FLOAT NOT NULL,
  volume FLOAT NULL, trade_count INT NULL, quality TINYINT NOT NULL,
  ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL,
  CONSTRAINT pk_bar_baseline PRIMARY KEY CLUSTERED (instrument_key, interval_code, ts, version)
) WITH (DATA_COMPRESSION = NONE);
