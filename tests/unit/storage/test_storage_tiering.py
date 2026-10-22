"""Tiering migration job: equality with the pure-SQL baseline, idempotency, resume, edge cases.

SQLite stands in for the hot store, ``LocalStore`` for Blob. The same flows against SQL Server and
Azurite (applock, blob index tags, duckdb azure reads) are in
``tests/integration/storage/test_tiering_integration.py``. One migrated reference world is built per
module; tests that mutate it work on a snapshot copy.
"""

# ruff: noqa: PLC0415, PT018
# ruff: noqa: F811 - the shared fixtures are imported by name and used as arguments

import asyncio
import hashlib
import io
import itertools
import json
import types
from collections.abc import Callable, Coroutine
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.exc import DBAPIError

from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.messages import TieringJob
from tidal.contracts.schemas import Interval
from tidal.query import duck
from tidal.query.reader import UnifiedReader
from tidal.storage import layout, tiering
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import write_partition
from tidal.storage.tiering import (
    BARS,
    TICKS,
    AppLock,
    DeleteChunk,
    LockLost,
    TieringConfig,
    TieringConflict,
    TieringDeleteMismatch,
    applock,
    chunk_bounds,
    plan_write,
    run_tiering,
)
from unit.query import query_support as qs
from unit.storage.tiering_support import (  # noqa: F401 - fixtures are imported by name
    CFG,
    CUTOFF,
    NOW,
    T0,
    TARGET,
    Boom,
    Env,
    baseline,
    cold_rows,
    env,
    migrated,
    ref,
    sql_rows,
    state,
    tree_hashes,
)

SETTINGS = Settings.from_env({"TIDAL_ENV": "local"})
DAY = qs.DAY


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def migrate(
    e: Env, cfg: TieringConfig = CFG, tables: tuple[str, ...] = ("bar_hot", "tick_hot")
) -> list[tiering.TableResult]:
    return run_tiering(e.engine, e.store, e.tagger, e.clock, cfg, tables)


def bars_result(results: list[tiering.TableResult]) -> tiering.TableResult:
    return next(r for r in results if r.table == "md.bar_hot")


# -- migration == baseline -----------------------------------------------------------------------
def test_target_is_the_hot_window_cutoff_rounded_down_to_a_month_start() -> None:
    assert TARGET <= CUTOFF < TARGET + 31 * DAY
    assert from_ns(TARGET) == (2020, 2, 1)


def from_ns(ns: int) -> tuple[int, int, int]:
    from tidal.common.timeutil import from_ts_ns

    m = from_ts_ns(ns)
    return m.year, m.month, m.day


def test_migration_moves_everything_below_the_target_and_loses_nothing(
    ref: Env, baseline: sr.SqlRepository
) -> None:
    remaining, cold = sql_rows(ref.repo), cold_rows(ref.store)
    assert remaining and cold
    assert all(r[1] >= TARGET for r in remaining)  # SQL: only the (month-aligned) hot window
    assert all(r[1] < TARGET for r in cold)
    assert sorted(remaining + cold) == sql_rows(baseline)  # row for row, every version, no dups
    results = ref.results
    assert all(not r.errors and not r.locked for r in results)
    assert sum(r.rows_moved for r in results) == sum(r.rows_deleted for r in results) == len(cold)
    assert {r.watermark_after for r in results} == {TARGET}
    assert ref.repo.get_watermark("md.bar_hot") == ref.repo.get_watermark("md.tick_hot") == TARGET


def test_reader_equals_baseline_after_migration(
    ref: Env, baseline: sr.SqlRepository, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    base_reader = UnifiedReader(baseline, duck.local_session(empty), FixedClock(NOW), SETTINGS)
    reader = UnifiedReader(ref.repo, duck.local_session(ref.root), FixedClock(NOW), SETTINGS)
    hi = T0 + 2_000 * DAY
    for interval in (Interval.D1, Interval.H1, Interval.M1):
        for as_of in (None, qs.INGEST_BASE + qs.ROUND, qs.LATE_INGEST):
            want = run(base_reader.read_bars(qs.WTI, interval, T0, hi, as_of_ns=as_of))
            got = run(reader.read_bars(qs.WTI, interval, T0, hi, as_of_ns=as_of))
            assert got.rows == want.rows
    want_t = run(base_reader.read_ticks(qs.WTI, qs.TICK0, qs.TICK0 + 60 * DAY))
    got_t = run(reader.read_ticks(qs.WTI, qs.TICK0, qs.TICK0 + 60 * DAY))
    assert got_t.rows == want_t.rows and len(want_t.rows) > 500


def test_parts_are_tagged_and_manifest_describes_them(ref: Env) -> None:
    parts = [p for p in ref.store.list("marketdata/v1/") if p.endswith(".parquet")]
    assert parts
    for path in parts:
        part = layout.parse_md_path(path)
        assert ref.tagger.get_tags(path) == {
            "dataset": part.dataset,
            "instrument": qs.WTI,
            "data_year": f"{part.year:04d}",
            "age_band": "hot",  # 2019/2020 relative to the 2020 clock: < 3 years
            "compacted": "false",
        }
    manifests = [p for p in ref.store.list("marketdata/v1/") if p.endswith("_manifest.json")]
    assert manifests
    for path in manifests:
        part = layout.parse_md_path(path)
        man = json.loads(ref.store.read(path))
        siblings = tiering.part_paths(ref.store, part.dataset, qs.WTI, part.year, part.month)
        assert [e["name"] for e in man["parts"]] == [s.rsplit("/", 1)[1] for s in siblings]
        rows = sum(
            pq.ParquetFile(io.BytesIO(ref.store.read(s))).metadata.num_rows for s in siblings
        )
        assert man["rows"] == rows and man["min_ts_ns"] <= man["max_ts_ns"]
        for e, s in zip(man["parts"], siblings, strict=True):
            assert e["sha256"] == hashlib.sha256(ref.store.read(s)).hexdigest()


def test_daily_bars_get_one_part_per_month_in_the_yearly_partition(ref: Env) -> None:
    assert len(tiering.part_paths(ref.store, "bars_1d", qs.WTI, 2019, 0)) == 3
    assert len(tiering.part_paths(ref.store, "bars_1d", qs.WTI, 2020, 0)) == 1  # January only


def test_rerun_is_a_noop_and_files_are_deterministic(
    ref: Env, baseline: sr.SqlRepository, tmp_path: Path
) -> None:
    first = tree_hashes(ref.root)
    again = migrate(ref.clone(tmp_path / "again"))
    assert all(r.rows_moved == r.rows_deleted == r.batches == 0 for r in again)
    other = Env(tmp_path / "other", baseline)
    migrate(other)
    assert tree_hashes(other.root) == first  # same inputs, same bytes


def test_page_and_chunk_sizes_do_not_change_the_result(ref: Env, env: Env) -> None:
    migrate(env, TieringConfig(90, batch_rows=97, delete_chunk_rows=40))
    assert tree_hashes(env.root) == tree_hashes(ref.root)  # one write per month regardless
    assert state(env) == state(ref)


def test_a_month_is_streamed_into_one_write_that_rolls_at_max_part_rows(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = write_partition
    calls: list[str] = []

    def spy(*a: Any, **k: Any) -> list[str]:
        calls.append(k["dataset"])
        return real(*a, max_part_rows=300, **k)

    monkeypatch.setattr(tiering, "write_partition", spy)
    migrate(env, tables=("tick_hot",))
    assert calls == ["ticks"]  # one call for the month
    assert len(tiering.part_paths(env.store, "ticks", qs.WTI, 2020, 1)) >= 3  # rolled
    assert sorted(sql_rows(env.repo) + cold_rows(env.store)) == sql_rows(env.baseline)


def test_watermark_is_monotone_and_never_lowered(migrated: Env) -> None:
    run_tiering(migrated.engine, migrated.store, migrated.tagger, FixedClock(NOW - 60 * DAY), CFG)
    assert migrated.repo.get_watermark("md.bar_hot") == TARGET


def test_dry_run_reports_without_changing_anything(
    env: Env, ref: Env, baseline: sr.SqlRepository
) -> None:
    before = state(env)
    results = migrate(env, TieringConfig(90, dry_run=True))
    assert state(env) == before and not (env.root / "marketdata").exists()
    assert all(r.dry_run and r.rows_deleted == 0 and r.watermark_after is None for r in results)
    real = ref.results
    assert sum(r.rows_moved for r in results) == sum(r.rows_moved for r in real)


def test_tiering_job_message_selects_the_table_by_its_contract_name(env: Env) -> None:
    job = TieringJob(table="md.tick_hot", hot_window_days=90, dry_run=False, batch_rows=1_000)
    res = tiering.run_job(env.engine, env.store, env.tagger, env.clock, job)
    assert res.table == "md.tick_hot" and res.rows_moved > 0
    assert env.repo.get_watermark("md.bar_hot") is None
    assert (
        tiering.spec_for_table("bar_hot") is BARS and tiering.spec_for_table("md.bar_hot") is BARS
    )
    with pytest.raises(layout.InvalidPath):
        tiering.spec_for_table("md.nope")


# -- restatements, duplicates, conflicts ---------------------------------------------------------
def test_restatement_ingested_after_migration_is_reversioned_and_converges(migrated: Env) -> None:
    ts = T0 + 10 * DAY
    old = [r for r in cold_rows(migrated.store) if r[0] == "bars_1d" and r[1] == ts]
    top = max(r[3] for r in old)
    # SQL no longer holds the key, so the repository files the restatement as version 1
    assert (
        migrated.repo.upsert_bars(
            qs.bars_table(np.array([ts]), np.array([7.0]), np.array([1.0]), qs.LATE_INGEST + 5)
        )
        == 1
    )
    res = bars_result(migrate(migrated))
    assert not res.errors and (res.rows_moved, res.rows_swept, res.rows_deleted) == (1, 1, 1)
    now = [r for r in cold_rows(migrated.store) if r[0] == "bars_1d" and r[1] == ts]
    assert len(now) == len(old) + 1
    new = max(now, key=lambda r: r[3])
    assert new[3] == top + 1 and new[4] == 7.0 and new[6] & 8  # RESTATED
    assert not any(r[1] < TARGET for r in sql_rows(migrated.repo))
    settled = state(migrated)
    migrate(migrated)
    assert state(migrated) == settled


def test_late_restatement_with_an_explicit_next_version_is_swept_into_a_new_part(
    migrated: Env,
) -> None:
    ts = T0 + 10 * DAY
    before = tiering.part_paths(migrated.store, "bars_1d", qs.WTI, 2019, 0)
    top = max(r[3] for r in cold_rows(migrated.store) if r[0] == "bars_1d" and r[1] == ts)
    late = pl.DataFrame(
        {
            "instrument_key": [1], "interval_code": [4], "ts_ns": [ts],
            "open": [7.0], "high": [7.0], "low": [7.0], "close": [7.0], "volume": [None],
            "trade_count": [None], "quality": [8], "ingested_at_ns": [qs.LATE_INGEST + 5],
            "version": [top + 1],
        },
        schema={
            c.name: pl.Float64 if c.name in ("open", "high", "low", "close", "volume") else pl.Int64
            for c in sr.bar_hot.c
        },
    )  # fmt: skip
    qs.insert_df(migrated.engine, sr.bar_hot, late)
    res = bars_result(migrate(migrated))
    assert (res.rows_moved, res.rows_swept, res.rows_deleted) == (1, 1, 1)
    after = tiering.part_paths(migrated.store, "bars_1d", qs.WTI, 2019, 0)
    assert len(after) == len(before) + 1 and all(p in after for p in before)  # nothing rewritten


def _write_dup_cold(e: Env, ingested_at: int) -> pl.DataFrame:
    """Cold copies of the hot v1 daily bars below the target, as if written by another run."""
    full = qs.raw_rows(e.repo, sr.bar_hot).filter(
        (pl.col("interval_code") == 4) & (pl.col("ts_ns") < TARGET) & (pl.col("version") == 1)
    )
    dup = qs.raw_to_contract(
        full.sort("ts_ns").with_columns(ingested_at_ns=pl.lit(ingested_at)), interval=Interval.D1
    )
    years = pl.Series(dup["ts_ns"].to_numpy()).cast(pl.Datetime("ns")).dt.year().to_numpy()
    for year in sorted(set(years.tolist())):
        write_partition(
            e.store, dataset="bars_1d", instrument_id=qs.WTI, year=int(year), month=0,
            batches=[dup.filter(pa.array(years == year))], source_hash="dup",
        )  # fmt: skip
    return full


def test_hot_rows_already_cold_from_an_earlier_ingest_are_deduped(env: Env) -> None:
    full = _write_dup_cold(env, 123)  # ingested before the hot rows: they add nothing
    res = bars_result(migrate(env))
    assert res.rows_deduped >= full.height
    unique_cold = {(r[0], r[1], r[2], r[3]) for r in cold_rows(env.store)}
    assert unique_cold == {
        (r[0], r[1], r[2], r[3]) for r in sql_rows(env.baseline) if r[1] < TARGET
    }
    assert not any(r[1] < TARGET for r in sql_rows(env.repo))


def test_as_of_reads_survive_when_cold_holds_a_later_ingest_of_the_same_bars(
    env: Env, tmp_path: Path
) -> None:
    """ADR-0013: a re-run with a LATER clock wrote the same bars to cold; hot ones are earlier."""
    _write_dup_cold(env, qs.LATE_INGEST + 10**15)
    asof = qs.INGEST_BASE + qs.ROUND
    before = UnifiedReader(env.repo, duck.local_session(env.root), FixedClock(NOW), SETTINGS)
    want = run(before.read_bars(qs.WTI, Interval.D1, T0, TARGET, as_of_ns=asof))
    assert want.rows
    res = bars_result(migrate(env))
    assert not res.errors
    after = UnifiedReader(env.repo, duck.local_session(env.root), FixedClock(NOW), SETTINGS)
    got = run(after.read_bars(qs.WTI, Interval.D1, T0, TARGET, as_of_ns=asof))
    assert got.rows == want.rows  # the earlier ingestion is still visible as of the past
    latest = run(after.read_bars(qs.WTI, Interval.D1, T0, TARGET))
    assert [r.close for r in latest.rows] == [
        r.close for r in run(before.read_bars(qs.WTI, Interval.D1, T0, TARGET)).rows
    ]


def test_cold_row_with_other_values_that_is_not_older_blocks_the_series(env: Env) -> None:
    ts = T0 + 5 * DAY
    bad = qs.bars_table(np.array([ts]), np.array([999.0]), np.array([1.0]), 9 * 10**18)
    write_partition(env.store, dataset="bars_1d", instrument_id=qs.WTI, year=2019, month=0,
                    batches=[bad], source_hash="bad")  # fmt: skip
    res = bars_result(migrate(env))
    assert res.errors == [f"{qs.WTI}: tiering-conflict"]
    assert env.repo.get_watermark("md.bar_hot") is None  # not advanced
    assert any(r[0] == "bars_1d" and r[1] == ts for r in sql_rows(env.repo))  # nothing deleted


def _frame(rows: list[tuple[int, int, float, int]], quality: int = 0) -> pl.DataFrame:
    """Bars frame from ``(ts, version, close, ingested_at)``; NaN volume, NULL trade_count."""
    ts, ver, px, ing = zip(*rows, strict=True)
    n = len(rows)
    return tiering.normalise(
        pl.DataFrame(
            {
                "ts_ns": ts, "open": px, "high": px, "low": px, "close": px,
                "volume": [float("nan")] * n, "trade_count": [None] * n,
                "quality": [quality] * n, "ingested_at_ns": ing, "version": ver,
            },
            schema_overrides={"trade_count": pl.Int64},
        )
    )  # fmt: skip


def test_plan_write_treats_nan_and_null_as_equal_and_other_versions_as_new() -> None:
    rows = _frame([(1, 1, 1.0, 5), (2, 1, 2.0, 5)])
    assert plan_write(rows, rows.clone(), BARS).height == 0
    assert plan_write(rows.with_columns(pl.col("version") + 1), rows, BARS).height == 2


def test_plan_write_ignores_a_cold_copy_ingested_later() -> None:
    hot = _frame([(1, 1, 1.0, 5)])
    assert plan_write(hot, _frame([(1, 1, 1.0, 9)]), BARS).height == 1
    assert plan_write(hot, _frame([(1, 1, 1.0, 5)]), BARS).height == 0
    assert plan_write(hot, _frame([(1, 1, 1.0, 2)]), BARS).height == 0


def test_plan_write_reversions_newer_rows_that_clash_with_cold() -> None:
    cold = _frame([(1, 1, 1.0, 1), (1, 2, 2.0, 2)])
    hot = _frame([(1, 1, 5.0, 10), (1, 2, 6.0, 11), (9, 1, 9.0, 12)])
    out = plan_write(hot, cold, BARS)
    assert out.select("ts_ns", "version", "close").rows() == [(1, 3, 5.0), (1, 4, 6.0), (9, 1, 9.0)]
    assert out["quality"].to_list() == [8, 8, 0]  # RESTATED on the re-versioned rows only
    # once the re-versioned copies are cold the same hot rows are recognised and dropped
    assert plan_write(hot, pl.concat([cold, out.filter(pl.col("ts_ns") == 1)]), BARS).select(
        "ts_ns"
    ).to_series().to_list() == [9]


def test_plan_write_raises_when_the_clashing_hot_row_is_not_newer() -> None:
    cold = _frame([(1, 1, 1.0, 20)])
    with pytest.raises(TieringConflict):
        plan_write(_frame([(1, 1, 5.0, 10)]), cold, BARS)
    with pytest.raises(TieringConflict):
        plan_write(_frame([(1, 1, 5.0, 20)]), cold, BARS)  # equal is not newer


def test_tick_key_includes_the_side() -> None:
    rows = tiering.normalise(
        pl.DataFrame({"ts_ns": [1, 1], "side": [0, 1], "version": [1, 1], "price": [1.0, 2.0],
                      "size": [0.5, 0.5], "quality": [0, 0], "ingested_at_ns": [5, 5]})
    )  # fmt: skip
    assert plan_write(rows, rows.head(1), TICKS)["side"].to_list() == [1]


# -- delete safety, locking ----------------------------------------------------------------------
def test_delete_chunk_that_would_remove_unread_rows_rolls_back(env: Env) -> None:
    n = len(sql_rows(env.repo))
    with pytest.raises(TieringDeleteMismatch):  # the range holds several versions per day
        tiering.delete_chunk(env.engine, BARS, DeleteChunk(1, 4, T0, T0 + 5 * DAY, 1))
    assert len(sql_rows(env.repo)) == n  # rolled back


class FakeLock(AppLock):
    """Acquired lock whose ``check`` raises :class:`LockLost` from the ``lose_at``-th call."""

    def __init__(self, lose_at: int | None = None) -> None:
        super().__init__(None, "fake", True)
        self.calls, self.lose_at = 0, lose_at

    def check(self) -> None:
        self.calls += 1
        if self.lose_at is not None and self.calls >= self.lose_at:
            raise LockLost("lost")


def patch_lock(monkeypatch: pytest.MonkeyPatch, make: Callable[[str], AppLock]) -> list[str]:
    seen: list[str] = []

    @contextmanager
    def fake(_engine: Any, resource: str) -> Any:
        seen.append(resource)
        yield make(resource)

    monkeypatch.setattr(tiering, "applock", fake)
    return seen


def test_second_run_exits_when_the_applock_is_held(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_lock(monkeypatch, lambda r: AppLock(None, r, False))
    before = state(env)
    assert all(r.locked for r in migrate(env))
    assert state(env) == before


def test_the_applock_resource_is_per_table(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = patch_lock(monkeypatch, lambda _r: FakeLock())
    migrate(env)
    assert seen == ["tiering:md.bar_hot", "tiering:md.tick_hot"]


def test_losing_the_lock_stops_before_the_next_delete_chunk(
    env: Env, ref: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = sql_rows(env.repo)
    patch_lock(monkeypatch, lambda _r: FakeLock(lose_at=12))
    with pytest.raises(LockLost):
        migrate(env, TieringConfig(90, delete_chunk_rows=50))
    both = set(sql_rows(env.repo)) | set(cold_rows(env.store))
    assert both == set(before)  # nothing lost; a later run finishes the job
    monkeypatch.undo()
    migrate(env)
    assert state(env) == state(ref)


class _Conn:
    def __init__(self, mode: str = "Exclusive", fail_release: bool = False) -> None:
        self.mode, self.fail_release = mode, fail_release

    def exec_driver_sql(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if "sp_releaseapplock" in sql and self.fail_release:
            raise RuntimeError("connection is dead")
        value = self.mode if "APPLOCK_MODE" in sql else 0
        return types.SimpleNamespace(scalar=lambda: value)

    def commit(self) -> None:
        return None

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: object) -> None:
        return None


class _Engine:
    dialect = types.SimpleNamespace(name="mssql")

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def connect(self) -> _Conn:
        return self.conn


def test_a_failing_release_on_a_dead_connection_does_not_mask_the_original_error() -> None:
    engine: Any = _Engine(_Conn(fail_release=True))
    with pytest.raises(KeyError, match="original"), applock(engine, "r"):
        raise KeyError("original")


def test_lock_check_uses_applock_mode_of_the_owning_session() -> None:
    conn: Any = _Conn("NoLock")
    with pytest.raises(LockLost):
        AppLock(conn, "r", True).check()
    ok: Any = _Conn("Exclusive")
    AppLock(ok, "r", True).check()  # holds: fine
    broken: Any = _Conn()
    broken.exec_driver_sql = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dead"))
    with pytest.raises(LockLost):
        AppLock(broken, "r", True).check()


def test_sp_spaceused_failure_is_a_warning_not_an_error() -> None:
    class Denied(_Conn):
        def exec_driver_sql(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
            raise DBAPIError("EXEC sp_spaceused", {}, Exception("permission denied"))

    engine: Any = _Engine(Denied())
    assert tiering.space_used_kb(engine, "md.bar_hot") is None


# -- crash at every step -------------------------------------------------------------------------
def _flaky(
    target: Any, name: str, *, after: bool = False, on_call: int = 1
) -> Callable[[Any], None]:
    """Make ``target.name`` raise :class:`Boom` on its ``on_call``-th call (after running it)."""

    def install(mp: pytest.MonkeyPatch) -> None:
        real = getattr(target, name)
        calls = itertools.count(1)

        def wrapper(*a: Any, **k: Any) -> Any:
            if next(calls) == on_call:
                if after:
                    real(*a, **k)
                raise Boom
            return real(*a, **k)

        mp.setattr(target, name, wrapper)

    return install


def _manifest_gap(mp: pytest.MonkeyPatch, env: Env) -> None:
    """Crash between the manifest delete and the manifest put."""
    real = env.store.put_new

    def put_new(path: str, local: Path) -> None:
        if path.endswith(layout.MANIFEST_NAME):
            raise Boom
        real(path, local)

    mp.setattr(env.store, "put_new", put_new)


CRASHES: dict[str, Callable[[pytest.MonkeyPatch, Env], None]] = {
    "before-write": lambda mp, e: _flaky(tiering, "write_partition")(mp),
    "after-write": lambda mp, e: _flaky(tiering, "tag_partition")(mp),
    "before-manifest": lambda mp, e: _flaky(tiering, "update_manifest")(mp),
    "manifest-delete-then-put": _manifest_gap,
    "before-delete": lambda mp, e: _flaky(tiering, "delete_chunk")(mp),
    "between-delete-chunks": lambda mp, e: _flaky(tiering, "delete_chunk", after=True, on_call=2)(
        mp
    ),
    "after-first-delete-chunk": lambda mp, e: _flaky(tiering, "delete_chunk", after=True)(mp),
    "before-watermark": lambda mp, e: _flaky(sr.SqlRepository, "set_watermark")(mp),
}


@pytest.mark.parametrize("step", sorted(CRASHES))
def test_crash_at_each_step_then_rerun_reaches_the_same_final_state(
    step: str, env: Env, ref: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same final state = same SQL rows, same cold rows (versions, ingested_at, quality), same
    watermarks; here also the same bytes and tags, because a re-run writes nothing twice."""
    cfg = TieringConfig(90, delete_chunk_rows=25)
    ticks = ("tick_hot",)  # the step logic is shared by both tables; ticks keep this fast
    before = set(sql_rows(env.repo))
    CRASHES[step](monkeypatch, env)
    with pytest.raises(Boom):
        migrate(env, cfg, ticks)
    assert set(sql_rows(env.repo)) | set(cold_rows(env.store)) == before  # nothing lost
    monkeypatch.undo()
    migrate(env, cfg, ticks)  # resume
    assert state(env, "ticks") == state(ref, "ticks")
    assert tree_hashes(env.root, "ticks") == tree_hashes(ref.root, "ticks")
    for path in (p for p in env.store.list("marketdata/v1/ticks/") if p.endswith(".parquet")):
        assert env.tagger.get_tags(path) == ref.tagger.get_tags(path)


@pytest.mark.parametrize(
    "step", ["after-write", "manifest-delete-then-put", "after-first-delete-chunk"]
)
def test_crash_while_migrating_a_reversioned_restatement_converges(
    step: str, migrated: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ts = T0 + 10 * DAY
    bar = qs.bars_table(np.array([ts]), np.array([7.0]), np.array([1.0]), qs.LATE_INGEST + 5)
    twin = migrated.clone(tmp_path / "twin")
    for e in (migrated, twin):
        e.repo.upsert_bars(bar)
    migrate(twin)
    CRASHES[step](monkeypatch, migrated)
    with pytest.raises(Boom):
        migrate(migrated)
    monkeypatch.undo()
    migrate(migrated)
    assert state(migrated) == state(twin)  # the restatement exists once, at one version


def test_a_corrupt_write_is_deleted_before_the_error_is_raised(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = write_partition

    def corrupt(store: Any, *, batches: Any, **kw: Any) -> list[str]:
        def bad() -> Any:
            for t in batches:
                close = t.column("close").to_numpy().copy() + 1.0
                field = t.schema.field("close")
                yield t.set_column(t.schema.get_field_index("close"), field, pa.array(close))

        return real(store, batches=bad(), **kw)

    before = state(env)
    monkeypatch.setattr(tiering, "write_partition", corrupt)
    res = bars_result(migrate(env, tables=("bar_hot",)))
    assert res.errors and all(e.endswith("tiering-verify-failed") for e in res.errors)
    assert not [p for p in env.store.list("marketdata/") if p.endswith(".parquet")]
    assert not list((env.root / ".tags").rglob("*.json")) if (env.root / ".tags").exists() else True
    assert state(env) == before and env.repo.get_watermark("md.bar_hot") is None


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


def test_overlapping_parts_uses_the_manifest_bounds(ref: Env) -> None:
    all_parts = tiering.part_paths(ref.store, "bars_1d", qs.WTI, 2019, 0)
    lo, hi = T0 + 40 * DAY, T0 + 50 * DAY
    some = tiering.overlapping_parts(ref.store, "bars_1d", qs.WTI, 2019, 0, (lo, hi))
    assert 1 <= len(some) < len(all_parts) == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"hot_window_days": 0},
        {"hot_window_days": 1, "batch_rows": 0},
        {"hot_window_days": 1, "delete_chunk_rows": 0},
    ],
)
def test_config_rejects_bad_values(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        TieringConfig(**bad)  # type: ignore[arg-type]
