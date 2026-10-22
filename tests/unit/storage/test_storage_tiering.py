"""Tiering migration job: equality with the pure-SQL baseline, idempotency, resume, edge cases.

SQLite stands in for the hot store, ``LocalStore`` for Blob. The same flows against SQL Server and
Azurite (applock, blob index tags) are in ``tests/integration/storage/test_tiering_integration.py``.
"""

# ruff: noqa: PLC0415, PT018

import asyncio
import io
import itertools
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow.parquet as pq
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import Interval
from tidal.query import duck
from tidal.query.reader import UnifiedReader
from tidal.storage import layout, tiering
from tidal.storage import sql_repo as sr
from tidal.storage.tiering import (
    BARS,
    TICKS,
    TieringConfig,
    TieringConflict,
    TieringDeleteMismatch,
    chunk_bounds,
    plan_write,
    run_tiering,
)
from unit.query import query_support as qs
from unit.storage.tiering_support import (
    CFG,
    CUTOFF,
    NOW,
    Boom,
    Env,
    cold_rows,
    sql_rows,
    tree_hashes,
)

SETTINGS = Settings.from_env({"TIDAL_ENV": "local"})


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def baseline() -> sr.SqlRepository:
    return qs.build_baseline()


@pytest.fixture
def env(tmp_path: Path, baseline: sr.SqlRepository) -> Env:
    return Env(tmp_path / "cold", baseline)


def migrate(e: Env, cfg: TieringConfig = CFG) -> list[tiering.TableResult]:
    return run_tiering(e.engine, e.store, e.tagger, e.clock, cfg)


def all_state(e: Env) -> list[tuple[Any, ...]]:
    return sorted(set(sql_rows(e.repo)) | set(cold_rows(e.store)))


# -- migration == baseline -----------------------------------------------------------------------
def test_migration_moves_everything_below_cutoff_and_loses_nothing(
    env: Env, baseline: sr.SqlRepository
) -> None:
    before = sql_rows(env.repo)
    results = migrate(env)
    assert all(not r.errors and not r.locked for r in results)
    remaining, cold = sql_rows(env.repo), cold_rows(env.store)
    assert remaining and all(r[1] >= CUTOFF for r in remaining)  # SQL: only the hot window
    assert all(r[1] < CUTOFF for r in cold)
    assert sorted(remaining + cold) == before  # row for row, every version kept, no duplicates
    assert sum(r.rows_moved for r in results) == len(cold)
    assert sum(r.rows_deleted for r in results) == len(cold)
    assert {r.watermark_after for r in results} == {CUTOFF}
    assert env.repo.get_watermark("md.bar_hot") == env.repo.get_watermark("md.tick_hot") == CUTOFF


def test_reader_equals_baseline_before_and_after_migration(
    env: Env, baseline: sr.SqlRepository, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    base_reader = UnifiedReader(baseline, duck.local_session(empty), FixedClock(NOW), SETTINGS)
    migrate(env)
    reader = UnifiedReader(env.repo, duck.local_session(env.root), FixedClock(NOW), SETTINGS)
    hi = qs.T0 + 2_000 * qs.DAY
    for interval in (Interval.D1, Interval.H1, Interval.M1):
        for as_of in (None, qs.INGEST_BASE + qs.ROUND, qs.LATE_INGEST):
            want = run(base_reader.read_bars(qs.WTI, interval, qs.T0, hi, as_of_ns=as_of))
            got = run(reader.read_bars(qs.WTI, interval, qs.T0, hi, as_of_ns=as_of))
            assert got.rows == want.rows
    want_t = run(base_reader.read_ticks(qs.WTI, qs.TICK0, qs.TICK0 + 60 * qs.DAY))
    got_t = run(reader.read_ticks(qs.WTI, qs.TICK0, qs.TICK0 + 60 * qs.DAY))
    assert got_t.rows == want_t.rows
    assert len(want_t.rows) > 100


def test_parts_are_tagged_and_manifest_describes_them(env: Env) -> None:
    migrate(env)
    parts = [p for p in env.store.list("marketdata/v1/") if p.endswith(".parquet")]
    assert parts
    for path in parts:
        tags = env.tagger.get_tags(path)
        part = layout.parse_md_path(path)
        assert tags == {
            "dataset": part.dataset,
            "instrument": qs.WTI,
            "data_year": f"{part.year:04d}",
            "age_band": "hot",  # 2019..2021 relative to the 2020/2021 clock: < 3 years
            "compacted": "false",
        }
    import json

    for path in env.store.list("marketdata/v1/"):
        if path.endswith("_manifest.json"):
            part = layout.parse_md_path(path)
            man = json.loads(env.store.read(path))
            siblings = tiering.part_paths(env.store, part.dataset, qs.WTI, part.year, part.month)
            assert [e["name"] for e in man["parts"]] == [s.rsplit("/", 1)[1] for s in siblings]
            total = sum(
                pq.ParquetFile(io.BytesIO(env.store.read(s))).metadata.num_rows for s in siblings
            )
            assert man["rows"] == total
            assert man["min_ts_ns"] <= man["max_ts_ns"]
            for e in man["parts"]:
                import hashlib

                data = env.store.read(path.rsplit("/", 1)[0] + "/" + e["name"])
                assert e["sha256"] == hashlib.sha256(data).hexdigest()


def test_daily_bars_get_one_part_per_month_in_the_yearly_partition(env: Env) -> None:
    migrate(env)
    ds = "bars_1d"
    names = tiering.part_paths(env.store, ds, qs.WTI, 2019, 0)
    assert len(names) == 12  # 2019-01 .. 2019-12, one appended part per month
    assert len(tiering.part_paths(env.store, ds, qs.WTI, 2020, 0)) >= 1


def test_rerun_is_a_noop_and_files_are_deterministic(
    env: Env, baseline: sr.SqlRepository, tmp_path: Path
) -> None:
    migrate(env)
    first = tree_hashes(env.root)
    again = migrate(env)
    assert all(r.rows_moved == r.rows_deleted == r.batches == 0 for r in again)
    assert tree_hashes(env.root) == first
    other = Env(tmp_path / "other", baseline)
    migrate(other)
    assert tree_hashes(other.root) == first  # same inputs, same bytes


def test_small_batches_and_chunks_give_the_same_rows(env: Env) -> None:
    before = sql_rows(env.repo)
    migrate(env, TieringConfig(90, batch_rows=700, delete_chunk_rows=300))
    assert sorted(sql_rows(env.repo) + cold_rows(env.store)) == before


def test_align_months_leaves_the_partial_month_in_sql(env: Env) -> None:
    results = migrate(env, TieringConfig(90, align_months=True))
    moment = pl.Series([CUTOFF]).cast(pl.Datetime("ns"))
    month_floor = int(
        pl.DataFrame({"t": moment}).select(pl.col("t").dt.truncate("1mo").dt.epoch("ns"))["t"][0]
    )
    assert {r.watermark_after for r in results} == {month_floor}
    assert min(r[1] for r in sql_rows(env.repo)) >= month_floor


def test_watermark_is_monotone_and_never_lowered(env: Env) -> None:
    migrate(env)
    earlier = FixedClock(NOW - 30 * qs.DAY)
    run_tiering(env.engine, env.store, env.tagger, earlier, CFG)
    assert env.repo.get_watermark("md.bar_hot") == CUTOFF


# -- late restatements, duplicates, conflicts ----------------------------------------------------
def test_late_restatement_below_watermark_is_swept_into_a_new_part(env: Env) -> None:
    migrate(env)
    ts = qs.T0 + 10 * qs.DAY
    latest = env.repo.read_bars(qs.WTI, Interval.D1, ts, ts + 1)
    assert latest.num_rows == 0  # migrated away
    seq_before = tiering.part_paths(env.store, "bars_1d", qs.WTI, 2019, 0)
    cold_latest = tiering.read_cold(env.store, seq_before, ["ts_ns", "version", "close"])
    row = cold_latest.filter(pl.col("ts_ns") == ts).sort("version").tail(1)
    late = pl.DataFrame(
        {
            "instrument_key": [1], "interval_code": [4], "ts_ns": [ts],
            "open": [7.0], "high": [7.0], "low": [7.0], "close": [7.0], "volume": [None],
            "trade_count": [None], "quality": [8], "ingested_at_ns": [qs.LATE_INGEST + 5],
            "version": [int(row["version"][0]) + 1],
        },
        schema={
            c.name: pl.Float64 if c.name in ("open", "high", "low", "close", "volume") else pl.Int64
            for c in sr.bar_hot.c
        },
    )  # fmt: skip
    qs.insert_df(env.engine, sr.bar_hot, late)
    res = migrate(env)
    bars = next(r for r in res if r.table == "md.bar_hot")
    assert (bars.rows_moved, bars.rows_swept, bars.rows_deleted) == (1, 1, 1)
    after = tiering.part_paths(env.store, "bars_1d", qs.WTI, 2019, 0)
    assert len(after) == len(seq_before) + 1  # a new part, nothing rewritten
    assert all(p in after for p in seq_before)
    assert not any(r[1] < CUTOFF for r in sql_rows(env.repo))


def test_hot_rows_already_cold_are_deduped_not_duplicated(
    env: Env, baseline: sr.SqlRepository
) -> None:
    """ADR-0013: a re-run with a later clock wrote the same bars to cold that were hot before."""
    import pyarrow as pa

    from tidal.storage.parquet_writer import write_partition

    full = qs.raw_rows(env.repo, sr.bar_hot).filter(
        (pl.col("interval_code") == 4) & (pl.col("ts_ns") < CUTOFF) & (pl.col("version") == 1)
    )
    # the same bars written by a run with another clock: only ingested_at_ns differs
    dup = qs.raw_to_contract(
        full.sort("ts_ns").with_columns(ingested_at_ns=pl.lit(123)), interval=Interval.D1
    )
    years = pl.Series(dup["ts_ns"].to_numpy()).cast(pl.Datetime("ns")).dt.year().to_numpy()
    for year in sorted(set(years.tolist())):
        write_partition(
            env.store, dataset="bars_1d", instrument_id=qs.WTI, year=int(year), month=0,
            batches=[dup.filter(pa.array(years == year))], source_hash="dup",
        )  # fmt: skip
    results = migrate(env)
    bars = next(r for r in results if r.table == "md.bar_hot")
    assert bars.rows_deduped >= full.height  # the v1 rows were already cold
    unique_cold = {(r[0], r[1], r[2], r[3]) for r in cold_rows(env.store)}
    want = {(r[0], r[1], r[2], r[3]) for r in sql_rows(baseline) if r[1] < CUTOFF}
    assert unique_cold == want  # the union converges to the baseline, no extra versions
    assert not any(r[1] < CUTOFF for r in sql_rows(env.repo))


def test_conflicting_cold_row_blocks_the_series_and_keeps_sql(env: Env) -> None:
    from tidal.storage.parquet_writer import write_partition

    ts = qs.T0 + 5 * qs.DAY
    bad = qs.bars_table(np.array([ts]), np.array([999.0]), np.array([1.0]), 1)
    write_partition(env.store, dataset="bars_1d", instrument_id=qs.WTI, year=2019, month=0,
                    batches=[bad], source_hash="bad")  # fmt: skip
    before = sql_rows(env.repo)
    bars = next(r for r in migrate(env) if r.table == "md.bar_hot")
    assert bars.errors == [f"{qs.WTI}: tiering-conflict"]
    assert env.repo.get_watermark("md.bar_hot") is None  # not advanced
    assert [r for r in sql_rows(env.repo) if r[0] == "bars_1d" and r[1] <= ts + qs.DAY] != []
    assert len(sql_rows(env.repo)) <= len(before)


def test_plan_write_treats_nan_and_null_as_equal() -> None:
    cols = {
        "ts_ns": [1, 2], "version": [1, 1], "open": [1.0, 2.0], "high": [1.0, 2.0],
        "low": [1.0, 2.0], "close": [1.0, 2.0], "volume": [float("nan"), 5.0],
        "trade_count": [None, 3],
    }  # fmt: skip
    rows = tiering.normalise(pl.DataFrame(cols))
    assert plan_write(rows, rows.clone(), BARS).height == 0
    other = rows.with_columns(pl.Series("volume", [float("nan"), 6.0]))
    with pytest.raises(TieringConflict):
        plan_write(rows, other, BARS)
    nxt = rows.with_columns(pl.col("version") + 1)
    assert plan_write(nxt, rows, BARS).height == 2  # other version -> still to write


def test_tick_dedupe_key_includes_side() -> None:
    rows = tiering.normalise(
        pl.DataFrame({"ts_ns": [1, 1], "side": [0, 1], "version": [1, 1],
                      "price": [1.0, 2.0], "size": [0.5, 0.5]})
    )  # fmt: skip
    cold = rows.head(1)
    assert plan_write(rows, cold, TICKS)["side"].to_list() == [1]


# -- delete safety, locking ----------------------------------------------------------------------
def test_delete_chunk_that_would_remove_unread_rows_rolls_back(env: Env) -> None:
    t = sr.bar_hot
    with env.engine.connect() as c:
        rows = pl.DataFrame(
            [
                dict(r)
                for r in c.execute(
                    select(t).where(t.c.interval_code == 4, t.c.ts_ns < qs.T0 + 5 * qs.DAY)
                ).mappings()
            ]
        )
    read = tiering.normalise(rows.filter(pl.col("version") == 1).sort("ts_ns", "version"))
    assert rows.height > read.height  # later versions exist in the range
    n = len(sql_rows(env.repo))
    with pytest.raises(TieringDeleteMismatch):
        tiering.delete_batch(env.engine, BARS, 1, 4, read, 50_000)
    assert len(sql_rows(env.repo)) == n  # rolled back


def test_second_run_exits_when_the_applock_is_held(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def held(_engine: Any, _resource: str) -> Any:
        yield False

    monkeypatch.setattr(tiering, "applock", held)
    before = sql_rows(env.repo)
    assert all(r.locked for r in migrate(env))
    assert sql_rows(env.repo) == before and not cold_rows(env.store)


def test_applock_resource_is_per_table(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    seen: list[str] = []

    @contextmanager
    def spy(_engine: Any, resource: str) -> Any:
        seen.append(resource)
        yield True

    monkeypatch.setattr(tiering, "applock", spy)
    migrate(env)
    assert seen == ["tiering:md.bar_hot", "tiering:md.tick_hot"]


# -- crash at every step -------------------------------------------------------------------------
def _flaky(module: Any, name: str, after: bool = False) -> Callable[[pytest.MonkeyPatch], None]:
    def install(mp: pytest.MonkeyPatch) -> None:
        real = getattr(module, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            if after:
                real(*a, **k)
            raise Boom

        mp.setattr(module, name, wrapper)

    return install


CRASHES = {
    "before-write": _flaky(tiering, "write_partition"),
    "after-write": _flaky(tiering, "tag_partition"),
    "before-manifest": _flaky(tiering, "update_manifest"),
    "before-delete": _flaky(tiering, "delete_batch"),
    "after-delete": _flaky(tiering, "delete_batch", after=True),
    "before-watermark": _flaky(sr.SqlRepository, "set_watermark"),
}


@pytest.mark.parametrize("step", sorted(CRASHES))
def test_crash_at_each_step_then_rerun_converges(
    step: str, env: Env, baseline: sr.SqlRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = all_state(env)
    ref = Env(tmp_path / "ref", baseline)
    migrate(ref)
    CRASHES[step](monkeypatch)
    with pytest.raises(Boom):
        migrate(env)
    assert all_state(env) == expected  # no row is lost at the crash point
    monkeypatch.undo()
    migrate(env)  # resume
    assert sorted(sql_rows(env.repo)) == sorted(sql_rows(ref.repo))
    assert sorted(set(cold_rows(env.store))) == sorted(set(cold_rows(ref.store)))
    assert len(cold_rows(env.store)) == len(cold_rows(ref.store))  # nothing written twice
    assert env.repo.get_watermark("md.bar_hot") == CUTOFF
    migrate(env)
    for path in env.store.list("marketdata/v1/"):
        if path.endswith(".parquet"):
            assert env.tagger.get_tags(path)["age_band"] == "hot"  # tagged before any delete


# -- helpers -------------------------------------------------------------------------------------
@given(
    st.lists(st.integers(0, 40), min_size=0, max_size=200).map(sorted),
    st.integers(1, 30),
)
def test_chunk_bounds_never_split_a_timestamp(values: list[int], size: int) -> None:
    ts = np.array(values, dtype=np.int64)
    chunks = chunk_bounds(ts, size)
    assert [i for a, b in chunks for i in range(a, b)] == list(range(len(ts)))
    for (_, b), (c, _) in itertools.pairwise(chunks):
        assert b == c and ts[b - 1] != ts[b]
    for a, b in chunks:
        assert int((ts[a:b] != ts[b - 1]).sum()) < size  # only the closing run may overshoot


@pytest.mark.parametrize(
    "bad",
    [
        {"hot_window_days": 0},
        {"hot_window_days": 1, "batch_rows": 0},
        {"hot_window_days": 1, "batch_rows": 10**9},
        {"hot_window_days": 1, "delete_chunk_rows": 0},
    ],
)
def test_config_rejects_bad_values(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be"):
        TieringConfig(**bad)  # type: ignore[arg-type]
