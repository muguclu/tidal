"""Unified reader: split logic, version resolution, tier and the 200-range baseline property.

SQLite stands in for the hot store and ``LocalStore`` + DuckDB for the cold one. The real SQL Server
and Azurite (duckdb ``azure`` extension) paths are covered by ``tests/integration/query``.

Two worlds back the properties. ``world``: Parquet below W_MAX, SQL from W_MIN plus late
restatements of cold keys (written to SQL only). ``mid_world``: a migration caught between "rows
deleted from SQL" and "watermark advanced", so SQL only holds rows from W_MAX while the watermark
is anywhere in ``[W_MIN, W_MAX]``. Both must equal the pure-SQL baseline.
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
from unit.query.query_support import (
    BAR_WM_RANGE,
    DAY,
    INGEST_BASE,
    INTRADAY,
    LATE_INGEST,
    N_DAYS,
    T0,
    TICK0,
    TICK_WM_RANGE,
    WTI,
    instrument_row,
    make_world,
    sqlite_engine,
)

SETTINGS = Settings.from_env({"TIDAL_ENV": "local"})


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
    def __init__(self, root: Path, *, mid_migration: bool = False) -> None:
        self.baseline, self.hot = make_world(pw.LocalStore(root), mid_migration=mid_migration)
        self.reader = UnifiedReader(self.hot, duck.local_session(root), FixedClock(0), SETTINGS)

    def set_wm(self, bars: int | None, ticks: int | None) -> None:
        for name, v in (("md.bar_hot", bars), ("md.tick_hot", ticks)):
            if v is not None:
                self.hot.set_watermark(name, v)

    async def stream_table(self, coro: Coroutine[Any, Any, RowStream]) -> tuple[Tier, pa.Table]:
        stream = await coro
        batches = [b async for b in stream.batches]
        return stream.tier, pa.Table.from_batches(batches) if batches else pa.table({})

    def expected_tier(
        self, want: pa.Table, hot_ts: list[Any], bounds: tuple[int, int], upper: int, wm: int
    ) -> Tier:
        """Independent tier rule: range vs watermark, mixed if SQL has rows below it or cold
        has rows at/above it (``want`` keys below ``upper`` are in Parquet)."""
        lo, hi = bounds
        ts = want["ts_ns"].to_numpy()
        cold_above = bool(((ts >= wm) & (ts < upper)).any())
        if any(t < wm for t in hot_ts) or cold_above:
            return Tier.MIXED
        return tier_for(lo, hi, wm)

    def check_bars(self, interval: Interval, lo: int, hi: int, as_of: int | None, wm: int) -> None:
        """Reader (rows and stream) must equal the baseline for one range."""
        want = self.baseline.read_bars(WTI, interval, lo, hi, as_of_ns=as_of)
        hot = self.hot.read_bars(WTI, interval, lo, hi, as_of_ns=as_of)
        rows = run(self.reader.read_bars(WTI, interval, lo, hi, as_of_ns=as_of))
        tier = self.expected_tier(want, hot["ts_ns"].to_pylist(), (lo, hi), BAR_WM_RANGE[1], wm)
        assert rows.tier is tier
        assert [r.ts_ns for r in rows.rows] == want["ts_ns"].to_pylist()
        assert [r.version for r in rows.rows] == want["version"].to_pylist()
        assert [r.close for r in rows.rows] == want["close"].to_pylist()
        stier, streamed = run(
            self.stream_table(self.reader.stream_bars(WTI, interval, lo, hi, as_of_ns=as_of))
        )
        assert stier is tier_for(lo, hi, wm)
        assert norm(streamed) == norm(want) if want.num_rows else streamed.num_rows == 0


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    w = World(tmp_path_factory.mktemp("cold"))
    w.set_wm(sum(BAR_WM_RANGE) // 2, sum(TICK_WM_RANGE) // 2)
    return w


@pytest.fixture(scope="module")
def mid_world(tmp_path_factory: pytest.TempPathFactory) -> World:
    return World(tmp_path_factory.mktemp("cold_mid"), mid_migration=True)


def test_world_has_restated_versions_and_negative_prices(world: World) -> None:
    got = world.baseline.read_bars(WTI, Interval.D1, T0, T0 + N_DAYS * DAY)
    assert got.column("version").to_numpy().max() >= 3  # restated in several ingest rounds
    assert (got.column("ingested_at_ns").to_numpy() == LATE_INGEST).any()  # late restatements
    assert got.column("close").to_numpy().min() < 0
    assert isinstance(world.reader, Reader)


# -- property: reader == pure-SQL baseline ----------------------------------------------------
AS_OF = st.one_of(st.none(), st.integers(INGEST_BASE - DAY, LATE_INGEST + DAY))


@settings(max_examples=200, deadline=None)
@given(
    wm=st.floats(0, 1),
    mid=st.booleans(),
    start=st.integers(-3 * DAY, (N_DAYS + 3) * DAY),
    length=st.integers(1, 1100 * DAY),
    as_of=AS_OF,
    tick_start=st.integers(-DAY, 8 * DAY),
    tick_len=st.integers(1, 9 * DAY),
)
def test_reader_equals_sql_baseline_over_random_ranges(
    world: World,
    mid_world: World,
    *,
    wm: float,
    mid: bool,
    start: int,
    length: int,
    as_of: int | None,
    tick_start: int,
    tick_len: int,
) -> None:
    w = mid_world if mid else world
    bw = int(BAR_WM_RANGE[0] + wm * (BAR_WM_RANGE[1] - BAR_WM_RANGE[0]))
    tw = int(TICK_WM_RANGE[0] + wm * (TICK_WM_RANGE[1] - TICK_WM_RANGE[0]))
    w.set_wm(bw, tw)
    lo = T0 + start
    w.check_bars(Interval.D1, lo, lo + length, as_of, bw)

    tlo, thi = TICK0 + tick_start, TICK0 + tick_start + tick_len
    twant = w.baseline.read_ticks(WTI, tlo, thi)
    thot = w.hot.read_ticks(WTI, tlo, thi)
    trows = run(w.reader.read_ticks(WTI, tlo, thi))
    ttier = w.expected_tier(twant, thot["ts_ns"].to_pylist(), (tlo, thi), TICK_WM_RANGE[1], tw)
    assert trows.tier is ttier
    assert [(r.ts_ns, int(r.side), r.version, r.price) for r in trows.rows] == list(
        zip(
            twant["ts_ns"].to_pylist(),
            twant["side"].to_pylist(),
            twant["version"].to_pylist(),
            twant["price"].to_pylist(),
            strict=True,
        )
    )
    stier, tstream = run(w.stream_table(w.reader.stream_ticks(WTI, tlo, thi)))
    assert stier is tier_for(tlo, thi, tw)
    if twant.num_rows:
        assert norm(tstream) == norm(twant)


@settings(max_examples=100, deadline=None)
@given(
    wm=st.floats(0, 1),
    mid=st.booleans(),
    interval=st.sampled_from(sorted(INTRADAY)),
    offset=st.integers(-2, 40),
    length_steps=st.integers(1, 3 * 24 * 60),
    as_of=AS_OF,
)
def test_monthly_partitioned_bars_across_a_year_boundary(
    world: World,
    mid_world: World,
    *,
    wm: float,
    mid: bool,
    interval: Interval,
    offset: int,
    length_steps: int,
    as_of: int | None,
) -> None:
    """1h / 1m bars live in month partitions; ranges cross 2019-12 / 2020-01 and the watermark."""
    w = mid_world if mid else world
    start, n, step = INTRADAY[interval]
    bw = int(BAR_WM_RANGE[0] + wm * (BAR_WM_RANGE[1] - BAR_WM_RANGE[0]))
    boundary = T0 + 365 * DAY  # 2020-01-01: every other example puts the watermark on it
    mark = boundary if offset % 2 else bw
    w.set_wm(mark, None)
    lo = start + (offset * n // 40) * step  # offset 40 = past the end of the data
    w.check_bars(interval, lo, lo + length_steps * step, as_of, mark)


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
    from unit.query.query_support import bars_table

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


def test_sessions_are_locked_down(tmp_path: Path) -> None:
    import duckdb

    local = duck.local_session(tmp_path)
    with pytest.raises(duckdb.Error, match="locked"):
        local.query("SET autoinstall_known_extensions = true", [])
    try:  # LOAD only (no INSTALL): works when the extension is already in the local cache
        az = duck.azure_session(
            account_url="http://127.0.0.1:10000/devstoreaccount1",
            managed_identity=False,
            connection_string="UseDevelopmentStorage=true",
        )
    except duck.ColdStoreUnavailable:
        pytest.skip("azure extension is not installed in this environment")
    with pytest.raises(duckdb.Error, match="locked"):
        az.query("SET azure_storage_connection_string = 'x'", [])
