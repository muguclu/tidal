"""Unified reader: split logic, version resolution, tier and the 200-range baseline property.

SQLite stands in for the hot store and ``LocalStore`` + DuckDB for the cold one. The real SQL Server
and Azurite (duckdb ``azure`` extension) paths are covered by ``tests/integration/query``.
"""

# ruff: noqa: PLC0415

import asyncio
import math
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from query_support import (
    BAR_WM_RANGE,
    DAY,
    INGEST_BASE,
    N_DAYS,
    T0,
    TICK0,
    TICK_WM_RANGE,
    WTI,
    build_baseline,
    instrument_row,
    split_stores,
    sqlite_engine,
)

from tidal.common.config import Settings
from tidal.common.errors import NotFound
from tidal.common.timeutil import FixedClock
from tidal.contracts.instruments import AssetClass, Source
from tidal.contracts.models import Tier
from tidal.contracts.schemas import Interval
from tidal.query import duck
from tidal.query.protocols import Reader, RowStream
from tidal.query.reader import UnifiedReader, resolve_versions, tier_for
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr

SETTINGS = Settings.from_env({"TIDAL_ENV": "local"})
ROUND = 10_000 * DAY


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def norm(table: pa.Table) -> list[tuple[Any, ...]]:
    """Rows as tuples with NaN mapped to ``None`` (NaN != NaN), schema-name order."""
    cols = [table.column(n).to_pylist() for n in table.schema.names]
    return [
        tuple(None if isinstance(v, float) and math.isnan(v) else v for v in row)
        for row in zip(*cols, strict=True)
    ]


class World:
    def __init__(self, root: Path) -> None:
        self.baseline = build_baseline()
        self.hot = split_stores(self.baseline, pw.LocalStore(root))
        self.reader = UnifiedReader(self.hot, duck.local_session(root), FixedClock(0), SETTINGS)

    def set_wm(self, bars: int | None, ticks: int | None) -> None:
        for name, v in (("md.bar_hot", bars), ("md.tick_hot", ticks)):
            if v is not None:
                self.hot.set_watermark(name, v)

    async def stream_table(self, coro: Coroutine[Any, Any, RowStream]) -> tuple[Tier, pa.Table]:
        stream = await coro
        batches = [b async for b in stream.batches]
        return stream.tier, pa.Table.from_batches(batches) if batches else pa.table({})


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    w = World(tmp_path_factory.mktemp("cold"))
    w.set_wm(sum(BAR_WM_RANGE) // 2, sum(TICK_WM_RANGE) // 2)
    return w


def test_world_has_restated_versions_and_negative_prices(world: World) -> None:
    got = world.baseline.read_bars(WTI, Interval.D1, T0, T0 + N_DAYS * DAY)
    assert got.column("version").to_numpy().max() == 3
    assert got.column("close").to_numpy().min() < 0
    assert isinstance(world.reader, Reader)


# -- property: reader == pure-SQL baseline ----------------------------------------------------
@settings(max_examples=200, deadline=None)
@given(
    wm=st.floats(0, 1),
    start=st.integers(-3 * DAY, (N_DAYS + 3) * DAY),
    length=st.integers(1, 1100 * DAY),
    as_of=st.one_of(st.none(), st.integers(INGEST_BASE - DAY, INGEST_BASE + 2 * ROUND + DAY)),
    tick_start=st.integers(-DAY, 8 * DAY),
    tick_len=st.integers(1, 9 * DAY),
)
def test_reader_equals_sql_baseline_over_random_ranges(
    world: World,
    *,
    wm: float,
    start: int,
    length: int,
    as_of: int | None,
    tick_start: int,
    tick_len: int,
) -> None:
    bw = int(BAR_WM_RANGE[0] + wm * (BAR_WM_RANGE[1] - BAR_WM_RANGE[0]))
    tw = int(TICK_WM_RANGE[0] + wm * (TICK_WM_RANGE[1] - TICK_WM_RANGE[0]))
    world.set_wm(bw, tw)
    lo, hi = T0 + start, T0 + start + length
    want = world.baseline.read_bars(WTI, Interval.D1, lo, hi, as_of_ns=as_of)
    rows = run(world.reader.read_bars(WTI, Interval.D1, lo, hi, as_of_ns=as_of))
    assert rows.tier is tier_for(lo, hi, bw)
    assert [r.ts_ns for r in rows.rows] == want["ts_ns"].to_pylist()
    assert [r.version for r in rows.rows] == want["version"].to_pylist()
    assert [r.close for r in rows.rows] == want["close"].to_pylist()
    tier, streamed = run(
        world.stream_table(world.reader.stream_bars(WTI, Interval.D1, lo, hi, as_of_ns=as_of))
    )
    assert tier is rows.tier
    if want.num_rows:
        assert norm(streamed) == norm(want)
    else:
        assert streamed.num_rows == 0

    tlo, thi = TICK0 + tick_start, TICK0 + tick_start + tick_len
    twant = world.baseline.read_ticks(WTI, tlo, thi)
    trows = run(world.reader.read_ticks(WTI, tlo, thi))
    assert trows.tier is tier_for(tlo, thi, tw)
    assert [(r.ts_ns, int(r.side), r.version) for r in trows.rows] == list(
        zip(
            twant["ts_ns"].to_pylist(),
            twant["side"].to_pylist(),
            twant["version"].to_pylist(),
            strict=True,
        )
    )
    ttier, tstream = run(world.stream_table(world.reader.stream_ticks(WTI, tlo, thi)))
    assert ttier is trows.tier
    if twant.num_rows:
        assert norm(tstream) == norm(twant)


# -- unit ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("start", "end", "wm", "tier"),
    [
        (0, 10, None, Tier.HOT),
        (10, 20, 10, Tier.HOT),
        (0, 10, 10, Tier.COLD),
        (0, 11, 10, Tier.MIXED),
        (5, 5, 10, Tier.HOT),
    ],
)
def test_tier_for(start: int, end: int, wm: int | None, tier: Tier) -> None:
    assert tier_for(start, end, wm) is tier


def test_resolve_versions_keeps_max_version_per_key() -> None:
    df = pl.DataFrame({"ts_ns": [2, 1, 1, 2], "version": [1, 2, 1, 3], "v": [1.0, 2.0, 3.0, 4.0]})
    out = resolve_versions(df, ["ts_ns"])
    assert out["ts_ns"].to_list() == [1, 2]
    assert out["v"].to_list() == [2.0, 4.0]


def test_cold_only_range_is_served_from_parquet_with_negative_value(tmp_path: Path) -> None:
    """WTI 2020-04-20 settled at -37.63; it must survive a cold read."""
    from query_support import bars_table

    day = 1_587_340_800 * 10**9
    store = pw.LocalStore(tmp_path)
    t = bars_table(
        np.array([day - DAY, day]), np.array([18.27, -37.63]), np.array([np.nan, 5.0]), 1
    )
    pw.write_partition(
        store,
        dataset="bars_1d",
        instrument_id=WTI,
        year=2020,
        month=0,
        batches=[t],
        source_hash="x",
    )
    repo = sr.SqlRepository(sqlite_engine())
    repo.upsert_instruments([instrument_row()])
    repo.set_watermark("md.bar_hot", day + DAY)
    reader = UnifiedReader(repo, duck.local_session(tmp_path), FixedClock(0), SETTINGS)
    got = run(reader.read_bars(WTI, Interval.D1, day - DAY, day + DAY))
    assert got.tier is Tier.COLD
    assert [b.close for b in got.rows] == [18.27, -37.63]
    assert [b.volume for b in got.rows] == [None, 5.0]
    assert got.rows[1].ts == datetime(2020, 4, 20, tzinfo=UTC)


def test_missing_cold_files_and_no_watermark(tmp_path: Path) -> None:
    repo = sr.SqlRepository(sqlite_engine())
    repo.upsert_instruments([instrument_row()])
    reader = UnifiedReader(repo, duck.local_session(tmp_path), FixedClock(0), SETTINGS)
    assert run(reader.read_bars(WTI, Interval.D1, 0, DAY)).tier is Tier.HOT
    repo.set_watermark("md.bar_hot", DAY)  # migrated range without files: empty, not an error
    empty = run(reader.read_bars(WTI, Interval.D1, 0, DAY))
    assert (empty.rows, empty.tier) == ([], Tier.COLD)
    assert run(reader.read_ticks(WTI, 0, DAY)).rows == []


def test_unknown_instrument_is_not_found(world: World) -> None:
    for coro in (
        world.reader.read_bars("EIA.NOPE.SPOT", Interval.D1, 0, DAY),
        world.reader.read_ticks("EIA.NOPE.SPOT", 0, DAY),
        world.reader.stream_bars("EIA.NOPE.SPOT", Interval.D1, 0, DAY),
        world.reader.stream_ticks("EIA.NOPE.SPOT", 0, DAY),
    ):
        with pytest.raises(NotFound):
            run(coro)


def test_stream_is_chunked_and_empty_range_yields_nothing(world: World) -> None:
    world.set_wm(BAR_WM_RANGE[0], TICK_WM_RANGE[0])
    stream = run(world.reader.stream_bars(WTI, Interval.D1, T0, T0 + 1000 * DAY))
    assert stream.tier is Tier.MIXED

    async def count() -> int:
        return sum([b.num_rows async for b in stream.batches])

    assert run(count()) == N_DAYS
    _, empty = run(
        world.stream_table(world.reader.stream_bars(WTI, Interval.D1, T0 - 9 * DAY, T0 - 8 * DAY))
    )
    assert empty.num_rows == 0


def test_latest_instruments_and_ping(world: World) -> None:
    last = run(world.reader.latest([WTI, "EIA.NOPE.SPOT"]))
    assert [x.instrument_id for x in last] == [WTI]
    assert last[0].bar.ts_ns == T0 + (N_DAYS - 1) * DAY
    assert run(world.reader.latest([WTI], Interval.S1)) == []
    assert [i.instrument_id for i in run(world.reader.instruments())] == [WTI]
    assert run(world.reader.instruments(asset_class=AssetClass.CRUDE, source=Source.EIA))
    assert run(world.reader.instruments(source=Source.FRED)) == []
    run(world.reader.ping())


def test_ping_fails_when_cold_root_is_missing(tmp_path: Path) -> None:
    repo = sr.SqlRepository(sqlite_engine())
    reader = UnifiedReader(
        repo.engine, duck.local_session(tmp_path / "absent"), FixedClock(0), SETTINGS
    )
    with pytest.raises(duck.ColdStoreUnavailable):
        run(reader.ping())


def test_duck_factory_paths_and_credential_checks(tmp_path: Path) -> None:
    assert duck.glob_for("bars_1d", WTI) == (
        "marketdata/v1/bars_1d/instrument=EIA.WTI.SPOT/year=*/month=*/part-*.parquet"
    )
    local = duck.session_from_settings(SETTINGS, local_root=tmp_path)
    assert local.uri("marketdata/v1/x") == (tmp_path / "marketdata/v1/x").as_posix()
    with pytest.raises(duck.ColdStoreUnavailable):  # no credentials: fails before any download
        duck.azure_session(
            account_url="http://127.0.0.1:10000/a", managed_identity=False, connection_string=None
        )
    with pytest.raises(duck.ColdStoreUnavailable):  # DDL value is validated, never interpolated raw
        duck.azure_session(
            account_url="https://x'; DROP--.blob.core.windows.net",
            managed_identity=True,
            connection_string=None,
        )
    with pytest.raises(duck.ColdStoreUnavailable):
        duck.session_from_settings(SETTINGS)  # local env without a connection string
