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

from tidal.contracts.schemas import BAR_V1, INTERVAL_CODES, TICK_V1, Interval
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr

WTI = "EIA.WTI.SPOT"
DAY = 86_400 * 10**9
HOUR = 3_600 * 10**9
T0 = 1_546_300_800 * 10**9  # 2019-01-01
N_DAYS = 1_000
TICK0 = 1_580_342_400 * 10**9  # 2020-01-30
N_TICKS = 6_000
BAR_WM_RANGE = (T0 + 300 * DAY, T0 + 700 * DAY)
TICK_WM_RANGE = (TICK0 + 2 * DAY, TICK0 + 6 * DAY)
ROUND = 10_000 * DAY
INGEST_BASE = T0 + ROUND  # ingest round k happens at INGEST_BASE + k * ROUND; k=3 is "late"
LATE_INGEST = INGEST_BASE + 3 * ROUND
#: Monthly-partitioned bars that cross the 2019/2020 year boundary: (start, rows, step).
INTRADAY = {
    Interval.H1: (T0 + 353 * DAY, 21 * 24, HOUR),  # 2019-12-20 .. 2020-01-10
    Interval.M1: (T0 + 364 * DAY + 12 * HOUR, 36 * 60, HOUR // 60),  # 2019-12-31 12:00 + 36 h
}


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


def bars_table(
    ts: np.ndarray,
    px: np.ndarray,
    vol: np.ndarray,
    ingested: int,
    interval: Interval = Interval.D1,
) -> pa.Table:
    n = len(ts)
    return pa.table(
        {
            "ts_ns": ts.astype(np.int64),
            "instrument_id": pa.array([WTI] * n).dictionary_encode().cast(BAR_V1.field(1).type),
            "interval": pa.array([str(interval)] * n)
            .dictionary_encode()
            .cast(BAR_V1.field(2).type),
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
    intra = {
        iv: (start + np.arange(n, dtype=np.int64) * step, rng.normal(30, 5, n))
        for iv, (start, n, step) in INTRADAY.items()
    }
    for rnd in range(3):
        ing = INGEST_BASE + rnd * ROUND
        pick = rng.random(N_DAYS) < (1.0 if rnd == 0 else 0.15)
        repo.upsert_bars(bars_table(ts[pick], px[pick] + rnd, vol[pick], ing))
        for iv, (its, ipx) in intra.items():
            ipick = rng.random(len(its)) < (1.0 if rnd == 0 else 0.1)
            repo.upsert_bars(bars_table(its[ipick], ipx[ipick] + rnd, ipx[ipick] * 0 + 1, ing, iv))
        tpick = rng.random(len(tts)) < (1.0 if rnd == 0 else 0.1)
        repo.upsert_ticks(ticks_table(tts[tpick], tside[tpick], tpx[tpick] + rnd, ing))
    return repo


def raw_rows(repo: sr.SqlRepository, table: Any) -> pl.DataFrame:
    """Every version of every row of ``bar_hot`` / ``tick_hot``."""
    with repo.engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(table)).mappings()]
    return pl.DataFrame(rows)


def insert_df(engine: Engine, table: Any, df: pl.DataFrame) -> None:
    with engine.begin() as c:
        for chunk in df.iter_slices(5_000):
            c.execute(insert(table), chunk.to_dicts())


def late_restatements(baseline: sr.SqlRepository) -> dict[str, pl.DataFrame]:
    """Version+1 rows (ingested late, price +100) for every 17th key below W_MIN of each table."""
    out: dict[str, pl.DataFrame] = {}
    for name, table, key, w_min in (
        ("bar", sr.bar_hot, ["interval_code", "ts_ns"], BAR_WM_RANGE[0]),
        ("tick", sr.tick_hot, ["ts_ns", "side"], TICK_WM_RANGE[0]),
    ):
        latest = raw_rows(baseline, table).sort(*key, "version").unique(key, keep="last")
        pick = latest.sort(*key).filter(pl.col("ts_ns") < w_min).gather_every(17)
        px = ["open", "high", "low", "close"] if name == "bar" else ["price"]
        out[name] = pick.with_columns(
            pl.col(*px) + 100.0,
            (pl.col("version") + 1).alias("version"),
            pl.lit(LATE_INGEST).alias("ingested_at_ns"),
        )
    return out


def raw_to_contract(df: pl.DataFrame, *, interval: Interval | None) -> pa.Table:
    """Raw SQL rows -> contract table (NULL volume -> NaN); ``interval=None`` means ticks."""
    n = df.height
    schema = TICK_V1 if interval is None else BAR_V1
    out: dict[str, Any] = {}
    for f in schema:
        if f.name == "instrument_id":
            out[f.name] = pa.array([WTI] * n).dictionary_encode()
        elif f.name == "interval":
            out[f.name] = pa.array([str(interval)] * n).dictionary_encode()
        elif f.name == "volume":
            out[f.name] = df["volume"].fill_null(float("nan")).to_arrow()
        else:
            out[f.name] = df[f.name].to_arrow()
    return pa.table({k: v.cast(schema.field(k).type) for k, v in out.items()}, schema=schema)


def make_world(
    store: pw.ObjectStore, *, mid_migration: bool
) -> tuple[sr.SqlRepository, sr.SqlRepository]:
    """Return ``(baseline, hot)``; ``store`` receives the cold Parquet tree.

    Normal: cold < W_MAX; hot >= W_MIN plus late restatements of cold-region keys (SQL only).
    Mid-migration: the late restatements were migrated too and SQL only keeps rows >= W_MAX (rows
    already deleted); the test leaves the watermark behind, so it does not cover them yet.
    """
    baseline = build_baseline()
    late = late_restatements(baseline)
    tables = {"bar": sr.bar_hot, "tick": sr.tick_hot}
    if mid_migration:
        for k, df in late.items():
            insert_df(baseline.engine, tables[k], df)
    write_cold(baseline, store)
    hot = sr.SqlRepository(sqlite_engine())
    hot.upsert_instruments([instrument_row()])
    load_hot(baseline, hot.engine, cutoff=1 if mid_migration else 0)
    if not mid_migration:
        for k, df in late.items():
            insert_df(baseline.engine, tables[k], df)
            insert_df(hot.engine, tables[k], df)
    return baseline, hot


def copy_rows(src: sr.SqlRepository, engine: Engine) -> None:
    """Copy every row (all versions) of ``src`` into ``engine`` (e.g. a SQL Server database)."""
    for table in (sr.bar_hot, sr.tick_hot):
        insert_df(engine, table, raw_rows(src, table).sort("ts_ns", "version"))


def load_hot(
    baseline: sr.SqlRepository, engine: Engine, *, cutoff: int = 0, everything: bool = False
) -> None:
    """Copy baseline rows at or after W_MIN (``cutoff=0``) / W_MAX (``1``), or all rows."""
    for table, bounds in ((sr.bar_hot, BAR_WM_RANGE), (sr.tick_hot, TICK_WM_RANGE)):
        part = raw_rows(baseline, table).sort("ts_ns", "version")
        if not everything:
            part = part.filter(pl.col("ts_ns") >= bounds[cutoff])
        insert_df(engine, table, part)


def write_cold(baseline: sr.SqlRepository, store: pw.ObjectStore) -> None:
    """Write baseline rows before W_MAX (all versions) as monthly/yearly Parquet partitions."""
    by_code = {code: iv for iv, code in INTERVAL_CODES.items()}
    for table, upper in ((sr.bar_hot, BAR_WM_RANGE[1]), (sr.tick_hot, TICK_WM_RANGE[1])):
        bars = table is sr.bar_hot
        df = raw_rows(baseline, table).filter(pl.col("ts_ns") < upper).sort("ts_ns", "version")
        dt = pl.from_epoch("ts_ns", time_unit="ns")
        daily = (pl.col("interval_code") == INTERVAL_CODES[Interval.D1]) if bars else pl.lit(False)
        df = df.with_columns(
            dt.dt.year().alias("_y"),
            pl.when(daily).then(0).otherwise(dt.dt.month()).alias("_m"),
            (pl.col("interval_code") if bars else pl.lit(0)).alias("_i"),
        )
        for (i, y, m), part in df.group_by(["_i", "_y", "_m"], maintain_order=True):
            iv = by_code[int(i)] if bars else None
            pw.write_partition(
                store, dataset=f"bars_{iv}" if bars else "ticks", instrument_id=WTI,
                year=int(y), month=int(m),
                batches=[raw_to_contract(part, interval=iv)], source_hash="t",
            )  # fmt: skip
