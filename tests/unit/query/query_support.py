"""Shared fixtures for the reader tests: a world of bars and ticks with restated versions.

One "baseline" SQL database holds every row (the pure-SQL answer). The reader's SQL holds only rows
at or after ``W_MIN`` and the cold Parquet tree holds rows before ``W_MAX``; the watermark is any
value in ``[W_MIN, W_MAX]``, so the two stores overlap like a half-finished migration.
"""

from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa
from sqlalchemy import Engine, create_engine, event, insert, select
from sqlalchemy.pool import StaticPool

from tidal.contracts.schemas import BAR_V1, TICK_V1
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr

WTI = "EIA.WTI.SPOT"
DAY = 86_400 * 10**9
T0 = 1_546_300_800 * 10**9  # 2019-01-01
N_DAYS = 1_000
TICK0 = 1_580_342_400 * 10**9  # 2020-01-30
N_TICKS = 6_000
BAR_WM_RANGE = (T0 + 300 * DAY, T0 + 700 * DAY)
TICK_WM_RANGE = (TICK0 + 2 * DAY, TICK0 + 6 * DAY)
INGEST_BASE = T0 + 10_000 * DAY  # ingest rounds happen at INGEST_BASE + k * 10_000 days


def instrument_row(iid: str = WTI) -> dict[str, object]:
    return {
        "instrument_id": iid, "source": "EIA", "asset_class": "crude", "kind": "spot",
        "unit": "USD/bbl", "currency": "USD", "exchange_tz": "America/Chicago",
        "granularity": "1d", "roll_method": None, "first_ts_ns": None, "last_ts_ns": None,
        "description": "EIA RWTC, spot, USD/bbl",
    }  # fmt: skip


def sqlite_engine() -> Engine:
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(eng, "connect")
    def _attach(dbapi: Any, _rec: object) -> None:
        for schema in ("ref", "md", "ops"):
            dbapi.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    sr.metadata.create_all(eng)
    return eng


def bars_table(ts: np.ndarray, px: np.ndarray, vol: np.ndarray, ingested: int) -> pa.Table:
    n = len(ts)
    return pa.table(
        {
            "ts_ns": ts.astype(np.int64),
            "instrument_id": pa.array([WTI] * n).dictionary_encode().cast(BAR_V1.field(1).type),
            "interval": pa.array(["1d"] * n).dictionary_encode().cast(BAR_V1.field(2).type),
            "open": px, "high": px, "low": px, "close": px, "volume": vol,
            "trade_count": pa.array([None] * n, pa.int32()),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, ingested, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=BAR_V1,
    )  # fmt: skip


def ticks_table(ts: np.ndarray, side: np.ndarray, px: np.ndarray, ingested: int) -> pa.Table:
    n = len(ts)
    return pa.table(
        {
            "ts_ns": ts.astype(np.int64),
            "instrument_id": pa.array([WTI] * n).dictionary_encode().cast(TICK_V1.field(1).type),
            "price": px, "size": np.full(n, 0.5, np.float32), "side": side.astype(np.int8),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, ingested, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=TICK_V1,
    )  # fmt: skip


def build_baseline(seed: int = 7) -> sr.SqlRepository:
    """SQL with every row: 3 ingest rounds, later ones restating a random subset."""
    rng = np.random.Generator(np.random.PCG64(seed))
    repo = sr.SqlRepository(sqlite_engine())
    repo.upsert_instruments([instrument_row()])
    ts = T0 + np.arange(N_DAYS, dtype=np.int64) * DAY
    px = np.round(rng.normal(20, 40, N_DAYS), 2)  # negative prices included
    vol = np.where(rng.random(N_DAYS) < 0.3, np.nan, np.round(rng.random(N_DAYS) * 1e4))
    gaps = rng.integers(0, 400, N_TICKS)  # zero gaps give equal ts_ns with different sides
    tts = TICK0 + np.cumsum(gaps).astype(np.int64) * 10**9
    tside = rng.integers(0, 3, N_TICKS)
    first = pl.DataFrame({"t": tts, "s": tside}).select(pl.struct("t", "s").is_first_distinct())
    keep = first.to_series().to_numpy()
    tts, tside = tts[keep], tside[keep]
    tpx = np.round(rng.normal(0, 5, len(tts)), 3)
    for rnd in range(3):
        ing = INGEST_BASE + rnd * 10_000 * DAY
        pick = rng.random(N_DAYS) < (1.0 if rnd == 0 else 0.15)
        repo.upsert_bars(bars_table(ts[pick], px[pick] + rnd, vol[pick], ing))
        tpick = rng.random(len(tts)) < (1.0 if rnd == 0 else 0.1)
        repo.upsert_ticks(ticks_table(tts[tpick], tside[tpick], tpx[tpick] + rnd, ing))
    return repo


def raw_rows(repo: sr.SqlRepository, table: Any) -> pl.DataFrame:
    """Every version of every row of ``bar_hot`` / ``tick_hot``."""
    with repo.engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(table)).mappings()]
    return pl.DataFrame(rows)


def to_contract(df: pl.DataFrame, *, bars: bool) -> pa.Table:
    """Raw SQL rows -> contract table (NULL volume -> NaN, instrument and interval attached)."""
    n = df.height
    schema = BAR_V1 if bars else TICK_V1
    out: dict[str, Any] = {}
    for f in schema:
        if f.name == "instrument_id":
            out[f.name] = pa.array([WTI] * n).dictionary_encode()
        elif f.name == "interval":
            out[f.name] = pa.array(["1d"] * n).dictionary_encode()
        elif f.name == "volume":
            out[f.name] = df["volume"].fill_null(float("nan")).to_arrow()
        else:
            out[f.name] = df[f.name].to_arrow()
    return pa.table({k: v.cast(schema.field(k).type) for k, v in out.items()}, schema=schema)


def split_stores(baseline: sr.SqlRepository, store: pw.ObjectStore) -> sr.SqlRepository:
    """Fill the cold ``store`` (rows before W_MAX) and return a repo with hot rows (>= W_MIN)."""
    hot = sr.SqlRepository(sqlite_engine())
    hot.upsert_instruments([instrument_row()])
    load_hot(baseline, hot.engine)
    write_cold(baseline, store)
    return hot


def load_hot(baseline: sr.SqlRepository, engine: Engine, *, everything: bool = False) -> None:
    """Copy baseline rows at or after W_MIN (or all rows) into ``engine`` (any dialect)."""
    for table, (w_min, _) in ((sr.bar_hot, BAR_WM_RANGE), (sr.tick_hot, TICK_WM_RANGE)):
        part = raw_rows(baseline, table).sort("ts_ns", "version")
        if not everything:
            part = part.filter(pl.col("ts_ns") >= w_min)
        with engine.begin() as c:
            for chunk in part.iter_slices(5_000):
                c.execute(insert(table), chunk.to_dicts())


def write_cold(baseline: sr.SqlRepository, store: pw.ObjectStore) -> None:
    """Write baseline rows before W_MAX (all versions) as monthly/yearly Parquet partitions."""
    for table, bars, dataset, (_, w_max) in (
        (sr.bar_hot, True, "bars_1d", BAR_WM_RANGE),
        (sr.tick_hot, False, "ticks", TICK_WM_RANGE),
    ):
        df = raw_rows(baseline, table).filter(pl.col("ts_ns") < w_max).sort("ts_ns", "version")
        dt = pl.from_epoch("ts_ns", time_unit="ns")
        df = df.with_columns(
            dt.dt.year().alias("_y"), (pl.lit(0) if bars else dt.dt.month()).alias("_m")
        )
        for (y, m), part in df.group_by(["_y", "_m"], maintain_order=True):
            pw.write_partition(
                store, dataset=dataset, instrument_id=WTI, year=int(y), month=int(m),
                batches=[to_contract(part, bars=bars)], source_hash="t",
            )  # fmt: skip
