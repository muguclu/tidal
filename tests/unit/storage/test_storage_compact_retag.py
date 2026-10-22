"""Compaction and yearly re-tagging: merge semantics, crash resume, idempotency, the CLI group."""

# ruff: noqa: PLC0415, PT018

import io
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
from tidal.storage import sql_repo as sr
from tidal.storage.compact import compact
from tidal.storage.parquet_writer import LocalStore, write_partition
from tidal.storage.retag import LocalTagger, age_band, desired_tags, ensure_tags, retag
from tidal.storage.tiering import run_tiering
from tidal.storage.tiering_cli import app
from unit.query import query_support as qs
from unit.storage.tiering_support import CFG, NOW, Boom, Env, cold_rows, tree_hashes

DS = "bars_1d"


@pytest.fixture(scope="module")
def baseline() -> sr.SqlRepository:
    return qs.build_baseline()


@pytest.fixture
def env(tmp_path: Path, baseline: sr.SqlRepository) -> Env:
    e = Env(tmp_path / "cold", baseline)
    run_tiering(e.engine, e.store, e.tagger, e.clock, CFG, ("bar_hot",))
    return e


def parts(e: Env, year: int = 2019) -> list[str]:
    return tiering.part_paths(e.store, DS, qs.WTI, year, 0)


def logical(e: Env) -> list[tuple[Any, ...]]:
    return sorted(set(cold_rows(e.store)))


def all_parts(e: Env) -> list[str]:
    return [p for p in e.store.list("marketdata/v1/") if p.endswith(".parquet")]


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
def test_retag_moves_blobs_across_band_boundaries_and_is_idempotent(env: Env) -> None:
    n = len(all_parts(env))
    first = retag(env.store, env.tagger, env.clock)
    assert (first.blobs, first.changed) == (n, 0)  # migration already tagged them
    later = FixedClock(NOW + 4 * 366 * 86_400 * 10**9)  # clock year 2025
    moved = retag(env.store, env.tagger, later)
    assert moved.changed == n  # every blob is 3+ years old now: hot -> cool
    assert env.tagger.get_tags(parts(env)[0])["age_band"] == "cool"
    assert retag(env.store, env.tagger, later).changed == 0


def test_retag_preserves_compacted_and_completes_missing_tags(env: Env) -> None:
    path = parts(env)[0]
    env.tagger.set_tags(path, {"compacted": "true"})
    retag(env.store, env.tagger, env.clock)
    tags = env.tagger.get_tags(path)
    assert tags["compacted"] == "true" and tags["age_band"] == "hot" and tags["dataset"] == DS
    assert not ensure_tags(env.tagger, path, 2020)


def test_retag_counts_blobs_deleted_underneath_it(env: Env) -> None:
    class Racy(LocalTagger):
        def get_tags(self, path: str) -> dict[str, str]:
            env.store.delete(path)
            return super().get_tags(path)

    res = retag(env.store, Racy(env.root), env.clock, datasets=(DS,))
    assert res.missing == res.blobs > 0


# -- compaction ----------------------------------------------------------------------------------
def test_compaction_merges_parts_keeping_every_version(env: Env) -> None:
    before = logical(env)
    assert len(parts(env)) == 12
    res = compact(env.engine, env.store, env.tagger, env.clock, DS)
    assert not res.errors and res.merged >= 1
    assert len(parts(env)) == 1 and len(parts(env, 2020)) == 1
    assert logical(env) == before and len(cold_rows(env.store)) == len(before)  # all versions
    path = parts(env)[0]
    assert env.tagger.get_tags(path)["compacted"] == "true"
    table = pq.read_table(io.BytesIO(env.store.read(path)))
    assert np.all(np.diff(table["ts_ns"].to_numpy()) >= 0)
    assert table["version"].to_numpy().max() >= 3
    assert (table.schema.metadata or {})[b"tidal.source_hash"].startswith(b"compact:")


def test_compaction_is_idempotent_and_byte_stable(env: Env) -> None:
    compact(env.engine, env.store, env.tagger, env.clock, DS)
    snap = tree_hashes(env.root)
    again = compact(env.engine, env.store, env.tagger, env.clock, DS)
    assert again.parts_written == again.parts_removed == 0
    assert tree_hashes(env.root) == snap


def test_compaction_collapses_exact_duplicates_only(env: Env) -> None:
    src = pq.read_table(io.BytesIO(env.store.read(parts(env)[0])))
    write_partition(env.store, dataset=DS, instrument_id=qs.WTI, year=2019, month=0,
                    batches=[src], source_hash="again")  # fmt: skip  # same rows, another part
    n_unique = len(logical(env))
    assert len(cold_rows(env.store)) > n_unique
    compact(env.engine, env.store, env.tagger, env.clock, DS)
    assert len(cold_rows(env.store)) == n_unique


@pytest.mark.parametrize("when", ["after-write", "mid-delete"])
def test_compaction_crash_then_rerun_converges(
    when: str, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = logical(env)
    real_delete = env.store.delete
    calls = {"n": 0}

    def flaky_delete(path: str) -> None:
        if when == "after-write" or calls["n"] >= 2:
            raise Boom
        calls["n"] += 1
        real_delete(path)

    monkeypatch.setattr(env.store, "delete", flaky_delete)
    with pytest.raises(Boom):
        compact(env.engine, env.store, env.tagger, env.clock, DS, year=2019)
    assert logical(env) == before  # duplicates across parts, but nothing lost
    monkeypatch.undo()
    res = compact(env.engine, env.store, env.tagger, env.clock, DS, year=2019)
    assert not res.errors and len(parts(env)) == 1
    assert logical(env) == before
    assert len(
        [r for r in cold_rows(env.store) if r[0] == DS and r[1] < qs.T0 + 365 * qs.DAY]
    ) == len([r for r in before if r[0] == DS and r[1] < qs.T0 + 365 * qs.DAY])


def test_compaction_never_deletes_parts_appended_after_the_snapshot(env: Env) -> None:
    compact(env.engine, env.store, env.tagger, env.clock, DS, year=2019)
    extra = qs.bars_table(np.array([qs.T0 + 3 * qs.DAY]), np.array([5.0]), np.array([1.0]), 9)
    extra = extra.set_column(
        extra.schema.get_field_index("version"), extra.schema.field("version"),
        pa.array([9], pa.int16()),
    )  # fmt: skip
    write_partition(env.store, dataset=DS, instrument_id=qs.WTI, year=2019, month=0,
                    batches=[extra], source_hash="late")  # fmt: skip
    assert len(parts(env)) == 2
    res = compact(env.engine, env.store, env.tagger, env.clock, DS, year=2019)
    assert res.merged == 1 and len(parts(env)) == 1
    assert any(r[3] == 9 for r in cold_rows(env.store))


def test_compaction_respects_a_held_lock(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def held(_e: Any, _r: str) -> Any:
        yield False

    monkeypatch.setattr(compact_mod, "applock", held)
    snap = tree_hashes(env.root)
    assert compact(env.engine, env.store, env.tagger, env.clock, DS).locked
    assert tree_hashes(env.root) == snap


def test_compaction_of_ticks_and_intraday(env: Env) -> None:
    run_tiering(env.engine, env.store, env.tagger, env.clock, CFG, ("tick_hot",))
    path = tiering.part_paths(env.store, "ticks", qs.WTI, 2020, 1)[0]
    table = pq.read_table(io.BytesIO(env.store.read(path)))
    env.store.delete(path)
    half = table.num_rows // 2
    for chunk in (table.slice(0, half), table.slice(half)):
        write_partition(env.store, dataset="ticks", instrument_id=qs.WTI, year=2020, month=1,
                        batches=[chunk], source_hash="h")  # fmt: skip
    before = logical(env)
    for dataset in ("ticks", "bars_1h", "bars_1m"):
        assert not compact(env.engine, env.store, env.tagger, env.clock, dataset).errors
    assert logical(env) == before
    assert len(tiering.part_paths(env.store, "ticks", qs.WTI, 2020, 1)) == 1


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


def test_default_taggers_build_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tidal.common.config import Settings
    from tidal.storage import tiering_cli

    monkeypatch.chdir(tmp_path)
    store, tagger = tiering_cli._build(Settings.from_env({"TIDAL_ENV": "local"}))
    assert isinstance(store, LocalStore) and isinstance(tagger, LocalTagger)
