"""Dataset builder, Format protocol and the W1-W6 workload registry with correctness oracles.

Dataset: deterministic from ``DatasetSpec`` (scale, seed, window) via the synthetic generator,
cached under ``.data/bench/<key>/`` in the F5 layout (written through ``parquet_writer``). W6
needs restatements: ``restate_rate`` of one instrument-month is re-published as ``version=2``
with a later ``ingested_at_ns``; they live in a sibling store so W2-W5 (which read
``version = 1``, the first print) are unaffected.

Oracles are computed with polars straight from the cached files, independent of any Format's
engine, so "all formats return identical aggregates" (SPEC-02 section 6) is checked per run.
"""

import hashlib
import json
import math
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import TICK_V1
from tidal.ingest.sources import synthetic
from tidal.storage import layout
from tidal.storage.parquet_writer import LocalStore, write_partition

DATA_ROOT: Final = Path(".data/bench")
NS_DAY: Final = 86_400 * 10**9
BASE_INGEST_NS: Final = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp()) * 10**9
RESTATE_INGEST_NS: Final = BASE_INGEST_NS + 31 * NS_DAY
DATASET_VERSION: Final = "1"
Value = dict[str, int | float]
Params = dict[str, Any]  # Any: JSON-able workload parameters (id lists, months, ns bounds)
Part = tuple[str, int, int]  # (instrument_id, year, month)


@dataclass(frozen=True)
class DatasetSpec:
    """Everything that determines the data; its hash is the cache key."""

    scale: str = "S"
    seed: int = 42
    start: date = date(2020, 1, 1)
    end: date = date(2024, 12, 31)
    n_instruments: int | None = None  # cap on the scale's instrument count (tiny test sets)
    restate_rate: float = 0.01

    def key(self) -> str:
        """16-hex digest of all parameters plus the dataset layout version."""
        blob = json.dumps([DATASET_VERSION, *self.__dict__.values()], default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Dataset:
    """A built (cached) dataset; ``instruments`` is the selection a Format should load."""

    root: Path
    spec: DatasetSpec
    units: tuple[Part, ...]  # every instrument-month of the build
    restated: Part  # the instrument-month holding the version=2 rows
    rows: dict[str, int]  # base rows per instrument
    content_hash: str
    instruments: tuple[str, ...]
    _mem: dict[tuple[Part, bool], list[pa.Table]] | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def base(self) -> LocalStore:
        """Store with the version=1 ticks."""
        return LocalStore(self.root / "base")

    @property
    def restate(self) -> LocalStore:
        """Store with the version=2 restatements."""
        return LocalStore(self.root / "restate")

    def select(self, instruments: list[str]) -> "Dataset":
        """View restricted to ``instruments`` (e.g. W1 loads only the first)."""
        return Dataset(
            self.root,
            self.spec,
            self.units,
            self.restated,
            self.rows,
            self.content_hash,
            tuple(instruments),
        )

    @property
    def months(self) -> list[tuple[int, int]]:
        """Sorted distinct (year, month) of the dataset."""
        return sorted({(y, m) for _, y, m in self.units})

    def partitions(self) -> list[Part]:
        """Selected (instrument, year, month) base partitions."""
        return [u for u in self.units if u[0] in self.instruments]

    def files(
        self, inst: str, months: list[tuple[int, int]], *, with_restated: bool = False
    ) -> list[Path]:
        """Cached Parquet files of one instrument over ``months``, in time order."""
        stores = (self.base, self.restate) if with_restated else (self.base,)
        return [
            store.root / p
            for y, m in months
            for store in stores
            for p in store.list(layout.md_partition_prefix("ticks", inst, y, m))
        ]

    def read_partition(self, part: Part, *, restated: bool = False) -> list[pa.Table]:
        """Tables of one partition (ascending ``ts_ns``), contract schema."""
        if self._mem is not None and (part, restated) in self._mem:
            return self._mem[(part, restated)]
        inst, y, m = part
        store = self.restate if restated else self.base
        paths = list(store.list(layout.md_partition_prefix("ticks", inst, y, m)))
        return [pq.read_table(store.root / p).cast(TICK_V1) for p in paths]

    def preloaded(self) -> "Dataset":
        """Copy holding the selected partitions in memory, so a timed ``load`` does no source I/O.

        Memory is the selected rows in Arrow form (about 40 B/row); W1 selects one instrument.
        """
        mem = {(p, False): self.read_partition(p) for p in self.partitions()}
        if self.restated[0] in self.instruments:
            mem[(self.restated, True)] = self.read_partition(self.restated, restated=True)
        return replace(self, _mem=mem)

    def scan(self, p: Params, *, with_restated: bool = False) -> pl.LazyFrame:
        """Lazy frame over ``p['instruments']`` x ``p['months']`` clipped to ``[lo_ns, hi_ns)``."""
        months = [(int(y), int(m)) for y, m in p["months"]]
        files = [
            f for i in p["instruments"] for f in self.files(i, months, with_restated=with_restated)
        ]
        lf = pl.scan_parquet(files)
        if p["lo_ns"] is not None:
            lf = lf.filter((pl.col("ts_ns") >= p["lo_ns"]) & (pl.col("ts_ns") < p["hi_ns"]))
        return lf


def _hash_store(store: LocalStore, h: "hashlib._Hash") -> None:
    for rel in store.list(layout.CONTAINER_MARKETDATA):
        t = pq.read_table(store.root / rel, columns=["ts_ns", "price", "version"])
        for c in ("ts_ns", "price", "version"):
            h.update(t.column(c).to_numpy().tobytes())


def build_dataset(spec: DatasetSpec, root: Path = DATA_ROOT) -> Dataset:
    """Build (or load from the on-disk cache) the dataset for ``spec`` under ``root/<key>/``."""
    out = root / spec.key()
    if not (out / "dataset.json").exists():
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".tmp-{spec.key()}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _generate(spec, tmp)  # a crash leaves only the temp dir, never a half-built cache
            if out.exists():  # stale dir without dataset.json
                shutil.rmtree(out, ignore_errors=True)
            tmp.rename(out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    d = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    units = tuple((i, int(y), int(m)) for i, y, m in d["units"])
    r = d["restated"]
    return Dataset(
        out,
        spec,
        units,
        (r[0], int(r[1]), int(r[2])),
        d["rows"],
        d["content_hash"],
        tuple(dict.fromkeys(u[0] for u in units)),
    )


def _generate(spec: DatasetSpec, out: Path) -> None:
    units = synthetic.plan(spec.scale, spec.start, spec.end)
    if spec.n_instruments is not None:
        keep = list(dict.fromkeys(u.instrument_id for u in units))[: spec.n_instruments]
        units = [u for u in units if u.instrument_id in keep]
    clock = FixedClock(BASE_INGEST_NS)
    base, restate = LocalStore(out / "base"), LocalStore(out / "restate")
    rows: dict[str, int] = {}
    for u in units:
        gen = synthetic.generate_unit(u, seed=spec.seed, scale=spec.scale, clock=clock)
        paths = write_partition(
            base,
            dataset="ticks",
            instrument_id=u.instrument_id,
            year=u.year,
            month=u.month,
            batches=gen,
            source_hash=spec.key(),
        )
        n = sum(pq.ParquetFile(base.root / p).metadata.num_rows for p in paths)
        rows[u.instrument_id] = rows.get(u.instrument_id, 0) + n
    months = sorted({(u.year, u.month) for u in units})
    ry, rm = months[len(months) // 2]
    restated = (units[0].instrument_id, ry, rm)
    _restate(spec, base, restate, restated)
    h = hashlib.sha256()
    _hash_store(base, h)
    _hash_store(restate, h)
    meta = {
        "units": [(u.instrument_id, u.year, u.month) for u in units],
        "restated": restated,
        "rows": rows,
        "content_hash": h.hexdigest(),
    }
    (out / "dataset.json").write_text(json.dumps(meta), encoding="utf-8")


def _restate(spec: DatasetSpec, base: LocalStore, store: LocalStore, part: Part) -> None:
    """Re-publish ``restate_rate`` of the partition as version 2 (new price, later ingest)."""
    inst, y, m = part
    paths = list(base.list(layout.md_partition_prefix("ticks", inst, y, m)))
    t = pa.concat_tables([pq.read_table(base.root / p).cast(TICK_V1) for p in paths])
    seq = np.random.SeedSequence(spec.seed, spawn_key=(0xBE9C, 1))
    rng = np.random.Generator(np.random.PCG64(seq))
    k = max(1, int(t.num_rows * spec.restate_rate))
    t = t.take(np.sort(rng.choice(t.num_rows, size=k, replace=False)))
    price = t.column("price").to_numpy()
    new = price + 0.005 * np.abs(price) * rng.standard_normal(k)
    for name, arr in (
        ("price", new),
        ("version", np.full(k, 2, dtype=np.int16)),
        ("ingested_at_ns", np.full(k, RESTATE_INGEST_NS, dtype=np.int64)),
    ):
        t = t.set_column(t.schema.get_field_index(name), TICK_V1.field(name), pa.array(arr))
    write_partition(
        store,
        dataset="ticks",
        instrument_id=inst,
        year=y,
        month=m,
        batches=[t],
        source_hash=spec.key(),
    )


@dataclass(frozen=True)
class LoadStats:
    """What a Format reports after ``load``."""

    rows: int
    bytes_written: int


@dataclass(frozen=True)
class WorkloadResult:
    """One query execution: oracle-comparable ``value`` plus engine-reported I/O."""

    value: Value
    rows_scanned: int | None = None
    bytes_read: int | None = None
    bytes_read_source: str = "n/a"


class Format(Protocol):
    """A storage candidate (F0-F6). ``storage_price_key`` is the prices.yaml at-rest meter."""

    id: str
    storage_price_key: str

    def load(self, dataset: Dataset) -> LoadStats:
        """Write ``dataset.instruments`` (base ticks plus restatements) into the store."""
        ...

    def size_bytes(self) -> int:
        """Bytes on storage right now."""
        ...

    def run(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON params
        """Execute W2-W6 (W1 is ``load``); value only, NO profiling inside the timed call."""
        ...

    def io_profile(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON
        """Untimed separate pass that fills ``bytes_read`` / ``rows_scanned`` (profiling on)."""
        ...

    def reopen(self) -> None:
        """Drop the connection and any cache the format controls (cold start)."""
        ...

    def teardown(self) -> None:
        """Delete everything the format created."""
        ...


@dataclass(frozen=True)
class Workload:
    """A named workload: definition for the report, parameter derivation and oracle."""

    id: str
    title: str
    definition: str
    params: Callable[[Dataset], Params]
    oracle: Callable[[Dataset, Params], Value]


def _bounds(year: int, month: int) -> tuple[int, int]:
    lo = datetime(year, month, 1, tzinfo=UTC)
    hi = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return int(lo.timestamp()) * 10**9, int(hi.timestamp()) * 10**9


def _p(
    insts: list[str], months: list[tuple[int, int]], rng: tuple[int, int] | None = None
) -> Params:
    lo, hi = rng if rng else (None, None)
    return {"instruments": insts, "months": [list(m) for m in months], "lo_ns": lo, "hi_ns": hi}


def _w1(ds: Dataset) -> Params:
    return _p(list(ds.instruments[:1]), ds.months)


def _w3(ds: Dataset) -> Params:
    return _p(list(ds.instruments[:1]), [ds.restated[1:]], _bounds(*ds.restated[1:]))


def _w4(ds: Dataset) -> Params:
    return _p(list(ds.instruments[:1]), ds.months[-1:])


def _w5(ds: Dataset) -> Params:
    return _p(list(ds.instruments[:10]), ds.months[-12:])


def _w6(ds: Dataset) -> Params:
    p = _p([ds.restated[0]], [ds.restated[1:]])
    p["asof_ns"] = [BASE_INGEST_NS + 15 * NS_DAY, BASE_INGEST_NS + 60 * NS_DAY]
    return p


_YEARS = {"S": 5, "M": 10, "L": 20}


def default_range(scale: str, end: date | None = None) -> tuple[date, date]:
    """``(start, end)`` covering the scale's years, ending at ``TIDAL_SYNTH__END`` (or 2025-12-31).

    S: last 5 years, M: last 10, L: last 20 (``synthetic.plan`` takes the last N years of range).
    """
    if end is None:
        env = os.environ.get("TIDAL_SYNTH__END")
        end = date.fromisoformat(env) if env else date(2025, 12, 31)
    return date(end.year - _YEARS[scale] + 1, 1, 1), end


def _first(lf: pl.LazyFrame) -> pl.LazyFrame:
    return lf.filter(pl.col("version") == 1)


def _o1(ds: Dataset, p: Params) -> Value:
    n = sum(ds.rows[i] for i in p["instruments"])
    if ds.restated[0] in p["instruments"]:
        n += _first_restated_count(ds)
    return {"rows": n}


def _first_restated_count(ds: Dataset) -> int:
    inst, y, m = ds.restated
    files = ds.files(inst, [(y, m)], with_restated=True)
    lf = pl.scan_parquet(files).filter(pl.col("version") == 2)
    return int(lf.select(pl.len()).collect().item())


def _o2(ds: Dataset, p: Params) -> Value:
    d = (
        _first(ds.scan(p))
        .filter(pl.col("side") == 0)
        .group_by(pl.col("ts_ns") // NS_DAY)
        .agg(
            o=pl.col("price").first(),
            h=pl.col("price").max(),
            lo=pl.col("price").min(),
            c=pl.col("price").last(),
            v=pl.col("size").cast(pl.Float64).sum(),
            n=pl.len(),
        )
        .collect()
    )
    return {
        "n_days": d.height,
        "n_trades": int(d["n"].sum()),
        "sum_open": float(d["o"].sum()),
        "sum_high": float(d["h"].sum()),
        "sum_low": float(d["lo"].sum()),
        "sum_close": float(d["c"].sum()),
        "sum_volume": float(d["v"].sum()),
    }


def _o3(ds: Dataset, p: Params) -> Value:
    d = (
        _first(ds.scan(p))
        .select(
            n=pl.len(), s=pl.col("price").sum(), lo=pl.col("price").min(), hi=pl.col("price").max()
        )
        .collect()
    )
    return {
        "n": d["n"][0],
        "sum_price": d["s"][0],
        "min_price": d["lo"][0],
        "max_price": d["hi"][0],
    }


def _o4(ds: Dataset, p: Params) -> Value:
    d = _first(ds.scan(p)).filter(pl.col("side") == 0).sort("ts_ns", "side").tail(1).collect()
    return {"ts_ns": d["ts_ns"][0], "price": d["price"][0]}


def _o5(ds: Dataset, p: Params) -> Value:
    out: Value = {}
    for i in p["instruments"]:
        q = {**p, "instruments": [i]}
        d = _first(ds.scan(q)).select(n=pl.len(), s=pl.col("price").sum()).collect()
        out[f"n:{i}"], out[f"sum_price:{i}"] = d["n"][0], d["s"][0]
    return out


def _o6(ds: Dataset, p: Params) -> Value:
    out: Value = {}
    for k, asof in enumerate(p["asof_ns"]):
        d = (
            ds.scan(p, with_restated=True)
            .filter(pl.col("ingested_at_ns") <= asof)
            .sort("ts_ns", "side", "version")
            .group_by("ts_ns", "side", maintain_order=True)
            .last()
            .select(n=pl.len(), s=pl.col("price").sum(), r=(pl.col("version") == 2).sum())
            .collect()
        )
        out[f"n_{k}"], out[f"sum_price_{k}"], out[f"n_restated_{k}"] = (
            d["n"][0],
            d["s"][0],
            d["r"][0],
        )
    return out


_DEFS = (
    ("W1", "Bulk write", "Write 1 instrument x all years of ticks (incl. restatements)", _w1, _o1),
    (
        "W2",
        "Full scan + daily OHLC",
        "Daily OHLC+volume, 1 instrument, all years (trades, v1)",
        _w1,
        _o2,
    ),
    ("W3", "1-month range", "Count/sum/min/max price, 1 instrument, middle month", _w3, _o3),
    ("W4", "Last trade price", "Latest first-print trade (side 0) of 1 instrument", _w4, _o4),
    (
        "W5",
        "Cross-instrument scan",
        "Count/sum price, up to 10 instruments x last 12 months",
        _w5,
        _o5,
    ),
    (
        "W6",
        "As-of with restatements",
        "Latest version per tick key (ts_ns, side) known at 2 ingest instants",
        _w6,
        _o6,
    ),
)
WORKLOADS: Final[dict[str, Workload]] = {d[0]: Workload(*d) for d in _DEFS}


def values_match(got: Value, want: Value, rel: float = 1e-9) -> bool:
    """Exact for ints, relative tolerance for floats (summation order may differ per engine)."""
    if got.keys() != want.keys():
        return False
    return all(
        math.isclose(float(got[k]), float(want[k]), rel_tol=rel, abs_tol=1e-9)
        if isinstance(want[k], float) or isinstance(got[k], float)
        else got[k] == want[k]
        for k in want
    )


def parse_ids(spec: str, prefix: str) -> list[str]:
    """``"W1,W3"`` or ``"W1..W4"`` -> ids (also used for formats ``F0..F6``)."""
    out: list[str] = []
    for part in spec.split(","):
        if ".." in part:
            a, b = part.split("..")
            out += [f"{prefix}{n}" for n in range(int(a[1:]), int(b[1:]) + 1)]
        else:
            out.append(part)
    return out
