"""Shared fixtures for the tiering/compaction/retag unit tests (SQLite hot store + LocalStore).

The world is a *small* version of ``unit.query.query_support.build_baseline`` (same generator,
fewer rows) so a full migration takes about a second: 150 daily bars from 2019-10-01 with three
ingest rounds (restated versions, negative prices, NULL volumes), a week of hourly and 13 hours of
minute bars across the 2019/2020 boundary, and about 4000 ticks from 2020-01-30.
"""

import hashlib
import io
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import polars as pl
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine

from tidal.common.timeutil import FixedClock, month_bounds
from tidal.contracts.schemas import INTERVAL_CODES, Interval
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import LocalStore, ObjectStore
from tidal.storage.retag import LocalTagger
from tidal.storage.tiering import TieringConfig
from unit.query import query_support as qs

DAY = qs.DAY
T0 = qs.T0 + 273 * DAY  # 2019-10-01: a short daily history keeps the number of monthly parts low
HOT_WINDOW_DAYS = 90
CUTOFF = qs.TICK0 + 7 * DAY  # 2020-02-06: hot window start
NOW = CUTOFF + HOT_WINDOW_DAYS * DAY
TARGET = month_bounds(2020, 2)[0]  # the cut-off rounded down to a month start
CFG = TieringConfig(hot_window_days=HOT_WINDOW_DAYS)
_BY_CODE = {code: iv for iv, code in INTERVAL_CODES.items()}


class Boom(BaseException):
    """Raised by the crash-injection patches (a killed process, not a ``TidalError``)."""


@contextmanager
def small_world() -> Iterator[None]:
    """Shrink the query-support generator constants while a baseline is built."""
    intraday = {
        Interval.H1: (T0 + 80 * DAY, 7 * 24, qs.HOUR),  # 2019-12-20 + 7 days
        Interval.M1: (T0 + 91 * DAY + 12 * qs.HOUR, 800, qs.HOUR // 60),  # crosses new year
    }
    with (
        mock.patch.object(qs, "T0", T0),
        mock.patch.object(qs, "N_DAYS", 150),
        mock.patch.object(qs, "N_TICKS", 4_000),
        mock.patch.object(qs, "INTRADAY", intraday),
    ):
        yield


def build_small_baseline() -> sr.SqlRepository:
    """All rows of the small world in one SQLite database."""
    with small_world():
        return qs.build_baseline()


def copy_sql(src: Engine, dst: Engine) -> None:
    """Exact copy of every schema of an in-memory SQLite hot store (serialize/deserialize)."""
    with src.connect() as a, dst.connect() as b:
        raw_src, raw_dst = a.connection.driver_connection, b.connection.driver_connection
        for schema in ("ref", "md", "ops"):
            raw_dst.deserialize(raw_src.serialize(name=schema), name=schema)  # type: ignore[union-attr]


class Env:
    """A hot store, a local cold tree and a tagger; ``load`` copies every baseline row into SQL."""

    def __init__(
        self, root: Path, baseline: sr.SqlRepository, *, load: bool = True, now_ns: int = NOW
    ) -> None:
        self.root = root
        self.store = LocalStore(root)
        self.tagger = LocalTagger(root)
        self.clock = FixedClock(now_ns)
        self.baseline = baseline
        self.results: list[Any] = []  # TableResults of the last reference migration
        self.repo = sr.SqlRepository(qs.sqlite_engine(), self.clock)
        self.repo.upsert_instruments([qs.instrument_row()])
        if load:
            copy_sql(baseline.engine, self.repo.engine)

    @property
    def engine(self) -> Engine:
        return self.repo.engine

    def clone(self, root: Path) -> "Env":
        """Snapshot copy: same cold tree and tag sidecars, same SQL rows and watermarks."""
        shutil.copytree(self.root, root)
        other = Env(root, self.baseline, load=False, now_ns=self.clock.now_ns())
        copy_sql(self.engine, other.engine)
        return other


def tree_hashes(root: Path, dataset: str = "") -> dict[str, str]:
    """sha256 of every file under ``root/marketdata`` (the tag sidecars live elsewhere)."""
    base = root / "marketdata"
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(base.rglob("*"))
        if p.is_file() and f"/{dataset}" in p.relative_to(root).as_posix()
    }


def sql_rows(repo: sr.SqlRepository) -> list[tuple[Any, ...]]:
    """Every SQL row as ``(dataset, ts_ns, side, version, value, ingested_at_ns, quality)``."""
    out: list[tuple[Any, ...]] = []
    for r in qs.raw_rows(repo, sr.bar_hot).iter_rows(named=True):
        ds = f"bars_{_BY_CODE[r['interval_code']].value}"
        out.append((ds, r["ts_ns"], 0, r["version"], r["close"], r["ingested_at_ns"], r["quality"]))
    for r in qs.raw_rows(repo, sr.tick_hot).iter_rows(named=True):
        out.append(
            ("ticks", r["ts_ns"], r["side"], r["version"], r["price"],
             r["ingested_at_ns"], r["quality"])
        )  # fmt: skip
    return sorted(out)


def cold_rows(store: ObjectStore) -> list[tuple[Any, ...]]:
    """Every cold row in the same shape as :func:`sql_rows` (duplicates kept)."""
    out: list[tuple[Any, ...]] = []
    for dataset in layout.DATASETS:
        for path in store.list(f"marketdata/v1/{dataset}/"):
            if not path.endswith(".parquet"):
                continue
            df = pl.from_arrow(pq.read_table(io.BytesIO(store.read(path))))
            assert isinstance(df, pl.DataFrame)
            for r in df.iter_rows(named=True):
                value = r["price"] if dataset == "ticks" else r["close"]
                out.append(
                    (dataset, r["ts_ns"], r.get("side", 0), r["version"], value,
                     r["ingested_at_ns"], r["quality"])
                )  # fmt: skip
    return sorted(out)


def state(
    e: Env, dataset: str = ""
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int | None, int | None]:
    """SQL rows, cold rows and both watermarks: what "same final state" compares (SPEC-03 §6).

    ``dataset`` restricts the rows to datasets starting with it (``"ticks"``).
    """
    return (
        [r for r in sql_rows(e.repo) if r[0].startswith(dataset)],
        [r for r in cold_rows(e.store) if r[0].startswith(dataset)],
        None if dataset == "ticks" else e.repo.get_watermark("md.bar_hot"),
        None if dataset.startswith("bars") else e.repo.get_watermark("md.tick_hot"),
    )


# -- fixtures shared by the tiering test modules (import them by name) ---------------------------
@pytest.fixture(scope="module")
def baseline() -> sr.SqlRepository:
    """The small world, every row in SQL."""
    return build_small_baseline()


@pytest.fixture(scope="module")
def ref(baseline: sr.SqlRepository, tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The baseline migrated once (``Env.results`` holds the results); tests clone, never mutate."""
    from tidal.storage.tiering import run_tiering  # noqa: PLC0415

    e = Env(tmp_path_factory.mktemp("ref") / "cold", baseline)
    e.results = run_tiering(e.engine, e.store, e.tagger, e.clock, CFG)
    return e


@pytest.fixture
def env(baseline: sr.SqlRepository, tmp_path: Path) -> Env:
    """A fresh, unmigrated hot store."""
    return Env(tmp_path / "cold", baseline)


@pytest.fixture
def migrated(ref: Env, tmp_path: Path) -> Env:
    """A private snapshot copy of the migrated reference."""
    return ref.clone(tmp_path / "cold")
