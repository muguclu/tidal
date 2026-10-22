"""Shared fixtures for the tiering/compaction/retag unit tests (SQLite hot store + LocalStore)."""

import io
import math
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq
from sqlalchemy import Engine

from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import INTERVAL_CODES
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import LocalStore, ObjectStore
from tidal.storage.retag import LocalTagger
from tidal.storage.tiering import TieringConfig
from unit.query import query_support as qs

CUTOFF = qs.TICK0 + 7 * qs.DAY  # inside both the daily history and the tick burst
NOW = CUTOFF + 90 * qs.DAY
CFG = TieringConfig(hot_window_days=90)
_BY_CODE = {code: iv for iv, code in INTERVAL_CODES.items()}


class Boom(BaseException):
    """Raised by the crash-injection patches."""


class Env:
    """A fresh hot store holding every baseline row plus an empty cold tree."""

    def __init__(self, root: Path, baseline: sr.SqlRepository, now_ns: int = NOW) -> None:
        self.root = root
        self.store = LocalStore(root)
        self.tagger = LocalTagger(root)
        self.clock = FixedClock(now_ns)
        self.baseline = baseline
        self.repo = sr.SqlRepository(qs.sqlite_engine(), self.clock)
        self.repo.upsert_instruments([qs.instrument_row()])
        qs.copy_rows(baseline, self.repo.engine)

    @property
    def engine(self) -> Engine:
        return self.repo.engine


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root/marketdata`` (the tag sidecars live elsewhere)."""
    import hashlib  # noqa: PLC0415

    base = root / "marketdata"
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def _norm(v: Any) -> Any:
    return None if isinstance(v, float) and math.isnan(v) else v


def sql_rows(repo: sr.SqlRepository) -> list[tuple[Any, ...]]:
    """Every SQL row as ``(dataset, ts_ns, side, version, value, ingested_at_ns, quality)``."""
    out: list[tuple[Any, ...]] = []
    bars = qs.raw_rows(repo, sr.bar_hot)
    for r in bars.iter_rows(named=True):
        ds = f"bars_{_BY_CODE[r['interval_code']].value}"
        out.append((ds, r["ts_ns"], 0, r["version"], r["close"], r["ingested_at_ns"], r["quality"]))
    ticks = qs.raw_rows(repo, sr.tick_hot)
    for r in ticks.iter_rows(named=True):
        out.append(
            (
                "ticks",
                r["ts_ns"],
                r["side"],
                r["version"],
                r["price"],
                r["ingested_at_ns"],
                r["quality"],
            )
        )
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
