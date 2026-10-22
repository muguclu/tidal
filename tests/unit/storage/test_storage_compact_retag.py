"""Compaction and yearly re-tagging: merge semantics, crash resume, idempotency, the CLI group."""

# ruff: noqa: PLC0415, PT018
# ruff: noqa: F811 - the shared fixtures are imported by name and used as arguments

import io
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import Interval
from tidal.storage import compact as compact_mod
from tidal.storage import layout, tiering
from tidal.storage.compact import compact
from tidal.storage.parquet_writer import LocalStore, write_partition
from tidal.storage.retag import LocalTagger, age_band, desired_tags, ensure_tags, retag
from tidal.storage.tiering import AppLock
from tidal.storage.tiering_cli import app
from unit.query import query_support as qs
from unit.storage.tiering_support import (  # noqa: F401 - fixtures are imported by name
    NOW,
    T0,
    Boom,
    Env,
    baseline,
    cold_rows,
    env,
    migrated,
    ref,
    tree_hashes,
)

DS = "bars_1d"


def parts(e: Env, year: int = 2019) -> list[str]:
    return tiering.part_paths(e.store, DS, qs.WTI, year, 0)


def logical(e: Env) -> list[tuple[Any, ...]]:
    return sorted(set(cold_rows(e.store)))


def all_parts(e: Env) -> list[str]:
    return [p for p in e.store.list("marketdata/v1/") if p.endswith(".parquet")]


def sidecars(e: Env) -> set[str]:
    base = e.root / ".tags"
    return (
        {p.relative_to(base).as_posix() for p in base.rglob("*.json")} if base.exists() else set()
    )


def run_compact(e: Env, dataset: str = DS, **kw: Any) -> compact_mod.CompactResult:
    return compact(e.engine, e.store, e.tagger, e.clock, dataset, **kw)


def held(_engine: Any, resource: str) -> Any:
    @contextmanager
    def cm() -> Any:
        yield AppLock(None, resource, False)

    return cm()


# -- age bands -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("age", "band"),
    [(0, "hot"), (2, "hot"), (3, "cool"), (10, "cool"), (11, "cold"), (25, "cold")],
)
def test_age_band_boundaries(age: int, band: str) -> None:
    assert age_band(2026 - age, 2026) == band


@given(st.integers(1990, 2060), st.integers(0, 40))
def test_age_band_only_ever_gets_colder(year: int, extra: int) -> None:
    order = ["hot", "cool", "cold"]
    assert order.index(age_band(year, year + extra)) <= order.index(
        age_band(year, year + extra + 1)
    )


def test_desired_tags_names_are_the_lifecycle_contract() -> None:
    path = layout.md_part_path("bars_1d", "EIA.WTI.SPOT", 2005, 0, 3)
    assert desired_tags(path, 2026) == {
        "dataset": "bars_1d",
        "instrument": "EIA.WTI.SPOT",
        "data_year": "2005",
        "age_band": "cold",
        "compacted": "false",
    }


# -- retag ---------------------------------------------------------------------------------------
def test_retag_moves_blobs_across_band_boundaries_and_is_idempotent(migrated: Env) -> None:
    e = migrated
    n = len(all_parts(e))
    first = retag(e.store, e.tagger, e.clock, e.engine)
    assert (first.blobs, first.changed, first.skipped_datasets) == (n, 0, 0)  # already tagged
    later = FixedClock(NOW + 4 * 366 * 86_400 * 10**9)  # clock year 2024
    moved = retag(e.store, e.tagger, later, e.engine)
    assert moved.changed == n  # 2019/2020 data is 3+ years old: hot -> cool
    assert e.tagger.get_tags(parts(e)[0])["age_band"] == "cool"
    assert retag(e.store, e.tagger, later, e.engine).changed == 0


def test_retag_preserves_compacted_and_completes_missing_tags(migrated: Env) -> None:
    e = migrated
    path = parts(e)[0]
    e.tagger.set_tags(path, {"compacted": "true"})
    retag(e.store, e.tagger, e.clock, e.engine)
    tags = e.tagger.get_tags(path)
    assert tags["compacted"] == "true" and tags["age_band"] == "hot" and tags["dataset"] == DS
    assert not ensure_tags(e.tagger, path, 2020)


def test_retag_counts_blobs_deleted_underneath_it(migrated: Env) -> None:
    class Racy(LocalTagger):
        def get_tags(self, path: str) -> dict[str, str]:
            migrated.store.delete(path)
            return super().get_tags(path)

    res = retag(
        migrated.store, Racy(migrated.root), migrated.clock, migrated.engine, datasets=(DS,)
    )
    assert res.missing == res.blobs > 0


def test_retag_takes_the_table_lock_and_skips_datasets_that_are_busy(
    migrated: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    @contextmanager
    def spy(_engine: Any, resource: str) -> Any:
        seen.append(resource)
        yield AppLock(None, resource, resource.endswith("tick_hot"))

    monkeypatch.setattr(tiering, "applock", spy)
    res = retag(migrated.store, migrated.tagger, migrated.clock, migrated.engine)
    assert res.skipped_datasets == 4 and res.blobs == len(  # the four bars_* datasets are skipped
        [p for p in all_parts(migrated) if "/ticks/" in p]
    )
    assert set(seen) == {"tiering:md.bar_hot", "tiering:md.tick_hot"}


# -- compaction ----------------------------------------------------------------------------------
def test_compaction_merges_parts_keeping_every_version(migrated: Env) -> None:
    e = migrated
    before = logical(e)
    assert len(parts(e)) == 3
    res = run_compact(e)
    assert not res.errors and res.merged >= 1
    assert len(parts(e)) == 1 and len(parts(e, 2020)) == 1
    assert logical(e) == before and len(cold_rows(e.store)) == len(before)  # all versions
    path = parts(e)[0]
    assert e.tagger.get_tags(path)["compacted"] == "true"
    table = pq.read_table(io.BytesIO(e.store.read(path)))
    assert np.all(np.diff(table["ts_ns"].to_numpy()) >= 0)
    assert table["version"].to_numpy().max() >= 3
    assert (table.schema.metadata or {})[b"tidal.source_hash"].startswith(b"compact:")


def test_compaction_is_idempotent_and_byte_stable(migrated: Env) -> None:
    run_compact(migrated)
    snap = tree_hashes(migrated.root)
    again = run_compact(migrated)
    assert again.parts_written == again.parts_removed == 0
    assert tree_hashes(migrated.root) == snap


def test_compaction_deletes_the_tag_sidecars_of_the_parts_it_removes(migrated: Env) -> None:
    run_compact(migrated)
    live = set(all_parts(migrated))
    assert {f"{p}.json" for p in live if "/bars_1d/" in p} <= sidecars(migrated)
    assert not any(
        "/bars_1d/" in s and s.removesuffix(".json") not in live for s in sidecars(migrated)
    )


def test_compaction_collapses_equal_duplicates_and_keeps_the_earliest_ingest(
    migrated: Env,
) -> None:
    e = migrated
    before = logical(e)
    src = pq.read_table(io.BytesIO(e.store.read(parts(e)[0])))
    later = src.set_column(
        src.schema.get_field_index("ingested_at_ns"),
        src.schema.field("ingested_at_ns"),
        pa.array(np.full(src.num_rows, 9 * 10**18), pa.int64()),
    )
    write_partition(e.store, dataset=DS, instrument_id=qs.WTI, year=2019, month=0,
                    batches=[later], source_hash="again")  # fmt: skip  # same rows, ingested later
    assert len(cold_rows(e.store)) > len(before)
    assert not run_compact(e).errors
    assert (
        cold_rows(e.store) == before
    )  # the earliest ingested_at_ns survived (as-of keeps working)


def test_compaction_refuses_same_key_and_version_with_different_values(migrated: Env) -> None:
    e = migrated
    ts = T0 + 5 * qs.DAY
    version = max(r[3] for r in cold_rows(e.store) if r[0] == DS and r[1] == ts)
    bad = qs.bars_table(np.array([ts]), np.array([999.0]), np.array([1.0]), 5)
    bad = bad.set_column(
        bad.schema.get_field_index("version"), bad.schema.field("version"),
        pa.array([version], pa.int16()),
    )  # fmt: skip
    write_partition(e.store, dataset=DS, instrument_id=qs.WTI, year=2019, month=0,
                    batches=[bad], source_hash="bad")  # fmt: skip
    before = tree_hashes(e.root)
    res = run_compact(e)
    assert res.errors == [f"{qs.WTI}/2019/00: tiering-conflict"]
    assert tree_hashes(e.root) == before  # nothing deleted, nothing written


def test_compaction_over_max_part_rows_writes_several_parts_and_converges_after_a_crash(
    migrated: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = migrated
    before = logical(e)
    real_delete = e.store.delete

    def boom(path: str) -> None:
        raise Boom

    monkeypatch.setattr(e.store, "delete", boom)
    with pytest.raises(Boom):
        run_compact(e, year=2019, max_part_rows=40)  # new parts written, old ones still there
    monkeypatch.setattr(e.store, "delete", real_delete)
    assert logical(e) == before
    res = run_compact(e, year=2019, max_part_rows=40)
    assert not res.errors and res.parts_written == 0  # the complete output set was recognised
    out = parts(e)
    assert len(out) > 1
    rows = [pq.ParquetFile(io.BytesIO(e.store.read(p))).metadata.num_rows for p in out]
    assert max(rows) <= 40 and logical(e) == before
    assert all(e.tagger.get_tags(p)["compacted"] == "true" for p in out)
    assert run_compact(e, year=2019, max_part_rows=40).parts_removed == 0  # stable


@pytest.mark.parametrize("when", ["after-write", "mid-delete"])
def test_compaction_crash_then_rerun_converges(
    when: str, migrated: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = migrated
    before = logical(e)
    real_delete = e.store.delete
    calls = {"n": 0}

    def flaky_delete(path: str) -> None:
        if when == "after-write" or calls["n"] >= 1:
            raise Boom
        calls["n"] += 1
        real_delete(path)

    monkeypatch.setattr(e.store, "delete", flaky_delete)
    with pytest.raises(Boom):
        run_compact(e, year=2019)
    assert logical(e) == before  # duplicates across parts, but nothing lost
    monkeypatch.undo()
    res = run_compact(e, year=2019)
    assert not res.errors and len(parts(e)) == 1 and logical(e) == before
    assert len(cold_rows(e.store)) == len(before)


def test_a_corrupt_compacted_part_is_deleted_before_the_error_is_raised(
    migrated: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = migrated
    real = write_partition

    def corrupt(store: Any, *, batches: Any, **kw: Any) -> list[str]:
        bad = []
        for t in batches:
            field = t.schema.field("close")
            close = t.column("close").to_numpy().copy() + 1.0
            bad.append(t.set_column(t.schema.get_field_index("close"), field, pa.array(close)))
        return real(store, batches=bad, **kw)

    before_files, before_tags = tree_hashes(e.root), sidecars(e)
    monkeypatch.setattr(compact_mod, "write_partition", corrupt)
    res = run_compact(e)
    assert res.errors and all(x.endswith("tiering-verify-failed") for x in res.errors)
    assert tree_hashes(e.root) == before_files and sidecars(e) == before_tags


def test_compaction_never_deletes_parts_appended_after_the_snapshot(migrated: Env) -> None:
    e = migrated
    run_compact(e, year=2019)
    extra = qs.bars_table(np.array([T0 + 3 * qs.DAY]), np.array([5.0]), np.array([1.0]), 9)
    extra = extra.set_column(
        extra.schema.get_field_index("version"), extra.schema.field("version"),
        pa.array([9], pa.int16()),
    )  # fmt: skip
    write_partition(e.store, dataset=DS, instrument_id=qs.WTI, year=2019, month=0,
                    batches=[extra], source_hash="late")  # fmt: skip
    assert len(parts(e)) == 2
    res = run_compact(e, year=2019)
    assert res.merged == 1 and len(parts(e)) == 1
    assert any(r[3] == 9 for r in cold_rows(e.store))


def test_compaction_respects_a_held_lock(migrated: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compact_mod, "applock", held)
    snap = tree_hashes(migrated.root)
    assert run_compact(migrated).locked
    assert tree_hashes(migrated.root) == snap


def test_compaction_of_ticks_and_intraday(migrated: Env) -> None:
    e = migrated
    path = tiering.part_paths(e.store, "ticks", qs.WTI, 2020, 1)[0]
    table = pq.read_table(io.BytesIO(e.store.read(path)))
    e.store.delete(path)
    half = table.num_rows // 2
    for chunk in (table.slice(0, half), table.slice(half)):
        write_partition(e.store, dataset="ticks", instrument_id=qs.WTI, year=2020, month=1,
                        batches=[chunk], source_hash="h")  # fmt: skip
    before = logical(e)
    for dataset in ("ticks", "bars_1h", "bars_1m"):
        assert not run_compact(e, dataset).errors
    assert logical(e) == before
    assert len(tiering.part_paths(e.store, "ticks", qs.WTI, 2020, 1)) == 1


def test_interval_enum_matches_datasets() -> None:
    assert {f"bars_{i.value}" for i in Interval} <= set(layout.DATASETS)


# -- CLI -----------------------------------------------------------------------------------------
def test_cli_group_is_registered_with_three_commands() -> None:
    from tidal.cli import build_app

    out = CliRunner().invoke(build_app(), ["tiering", "--help"])
    assert out.exit_code == 0
    for cmd in ("run", "retag", "compact"):
        assert cmd in out.output
    assert CliRunner().invoke(app, ["run", "--table", "nope"]).exit_code != 0
    raw = CliRunner().invoke(app, ["run", "--help"], env={"COLUMNS": "200"}).output
    help_out = re.sub(r"\x1b\[[0-9;]*m", "", raw)  # CI renders Rich colours
    assert "--dry-run" in help_out and "--hot-window-days" in help_out


def test_default_taggers_build_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tidal.common.config import Settings
    from tidal.storage import tiering_cli

    monkeypatch.chdir(tmp_path)
    store, tagger = tiering_cli._build(Settings.from_env({"TIDAL_ENV": "local"}))
    assert isinstance(store, LocalStore) and isinstance(tagger, LocalTagger)
