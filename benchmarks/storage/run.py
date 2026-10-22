"""Benchmark runner and the reference format F5 (SPEC-02 section 5).

``uv run python -m benchmarks.storage.run --scale S --formats F5 --workloads W1..W6 --runs 5``
(plus ``--out DIR``; needs ``TIDAL_ENV``).

Cold = a fresh connection per run (the OS page cache is NOT dropped, so cold understates a truly
cold read); warm = one untimed priming run, then repeats on the same connection. W1 is a fresh
store per run. The data source (reading the cached dataset) is part of W1 wall time for every
format alike. Formats F0-F4/F6 plug in through ``FORMATS`` (T-202/T-203).
"""

import argparse
import json
import os
import platform
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import polars as pl
import pyarrow as pa
from benchmarks.storage.results_schema import ResultRow, write_results
from benchmarks.storage.workloads import (
    DATA_ROOT,
    WORKLOADS,
    Dataset,
    DatasetSpec,
    Format,
    LoadStats,
    WorkloadResult,
    build_dataset,
    parse_ids,
    values_match,
)

from tidal import __version__
from tidal.common.errors import ConfigError
from tidal.storage import layout
from tidal.storage.parquet_writer import LocalStore, write_partition

_W2 = (
    "SELECT count(*) AS n_days, sum(n) AS n_trades, sum(o) AS so, sum(h) AS sh, sum(l) AS sl,"
    " sum(c) AS sc, sum(v) AS sv FROM (SELECT arg_min(price, ts_ns) o, max(price) h, min(price) l,"
    " arg_max(price, ts_ns) c, sum(size::DOUBLE) v, count(*) n FROM read_parquet(?)"
    " WHERE version = 1 AND side = 0 GROUP BY ts_ns // 86400000000000)"  # 1 UTC day in ns
)
_W2_KEYS = ("n_days", "n_trades", "sum_open", "sum_high", "sum_low", "sum_close", "sum_volume")
_W3 = (
    "SELECT count(*), sum(price), min(price), max(price) FROM read_parquet(?)"
    " WHERE version = 1 AND ts_ns >= ? AND ts_ns < ?"
)


class OracleMismatchError(AssertionError):
    """A format returned a different aggregate than the independent polars oracle."""


class F5Format:
    """Reference format: epoch-optimised Parquet (storage-layout section 3) read with DuckDB."""

    id = "F5"
    storage_price_key = "blob_hot_stored_gb_month"

    def __init__(self, workdir: Path) -> None:
        """Create the format under ``workdir`` (deleted by :meth:`teardown`)."""
        self.workdir = workdir
        self.store = LocalStore(workdir / "f5")
        self._con: duckdb.DuckDBPyConnection | None = None
        self._bytes = 0
        self._rows = 0
        self._prof = str(workdir / "profile.json")

    def load(self, dataset: Dataset) -> LoadStats:
        """Write base partitions, then the restatement rows as an extra immutable part."""
        key, rows = dataset.spec.key(), 0
        for part in dataset.partitions():
            tables = dataset.read_partition(part)
            write_partition(self.store, dataset="ticks", instrument_id=part[0], year=part[1],
                            month=part[2], batches=tables, source_hash=key)  # fmt: skip
            rows += sum(t.num_rows for t in tables)
        if dataset.restated[0] in dataset.instruments:
            inst, y, m = dataset.restated
            tables = dataset.read_partition(dataset.restated, restated=True)
            write_partition(self.store, dataset="ticks", instrument_id=inst, year=y, month=m,
                            batches=tables, source_hash=key)  # fmt: skip
            rows += sum(t.num_rows for t in tables)
        return LoadStats(rows, self.size_bytes())

    def size_bytes(self) -> int:
        """Sum of all Parquet file sizes."""
        return sum(p.stat().st_size for p in (self.workdir / "f5").rglob("*") if p.is_file())

    def reopen(self) -> None:
        """New DuckDB connection with JSON profiling (source of bytes read / rows scanned)."""
        if self._con is not None:
            self._con.close()
        self._con = duckdb.connect()
        self._prof = str(self.workdir / "profile.json")
        self._con.execute("PRAGMA enable_profiling='json'")
        self._con.execute(f"PRAGMA profiling_output='{self._prof}'")

    def _q(self, sql: str, args: list[Any]) -> list[tuple[Any, ...]]:  # Any: duckdb cell values
        if self._con is None:
            self.reopen()
        assert self._con is not None
        out = self._con.execute(sql, args).fetchall()
        prof = json.loads(Path(self._prof).read_text(encoding="utf-8"))
        self._bytes += int(prof["io"]["total_bytes_read"])
        self._rows += int(prof["query"]["total_rows_scanned"])
        return out

    def _files(self, p: Mapping[str, Any]) -> list[str]:
        out: list[str] = []
        for inst in p["instruments"]:
            for y, m in p["months"]:
                out += self.store.list_paths(layout.md_partition_prefix("ticks", inst, y, m))
        return [str(self.store.root / f) for f in sorted(out)]

    def run(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON params
        """Run W2-W6 as DuckDB SQL over the partition files selected from the layout."""
        self._bytes = self._rows = 0
        files, p = [self._files(params)], params
        rng = [] if p["lo_ns"] is None else [p["lo_ns"], p["hi_ns"]]
        if workload == "W2":
            r = self._q(_W2, files)[0]
            v: dict[str, int | float] = dict(
                zip(_W2_KEYS, (int(r[0]), int(r[1]), *map(float, r[2:])), strict=True)
            )
        elif workload == "W3":
            r = self._q(_W3, [*files, *rng])[0]
            v = {
                "n": int(r[0]),
                "sum_price": float(r[1]),
                "min_price": float(r[2]),
                "max_price": float(r[3]),
            }
        elif workload == "W4":
            r = self._q("SELECT ts_ns, price FROM read_parquet(?) WHERE version = 1"
                        " ORDER BY ts_ns DESC LIMIT 1", files)[0]  # fmt: skip
            v = {"ts_ns": int(r[0]), "price": float(r[1])}
        elif workload == "W5":
            v = {}
            for inst in p["instruments"]:
                one = {**p, "instruments": [inst]}
                r = self._q("SELECT count(*), sum(price) FROM read_parquet(?) WHERE version = 1",
                            [self._files(one)])[0]  # fmt: skip
                v[f"n:{inst}"], v[f"sum_price:{inst}"] = int(r[0]), float(r[1])
        elif workload == "W6":
            v = {}
            for k, asof in enumerate(p["asof_ns"]):
                r = self._q(
                    "SELECT count(*), sum(price), count_if(version = 2) FROM (SELECT ts_ns,"
                    " arg_max(price, version) AS price, max(version) AS version"
                    " FROM read_parquet(?) WHERE ingested_at_ns <= ? GROUP BY ts_ns)",
                    [*files, asof],
                )[0]
                v[f"n_{k}"], v[f"sum_price_{k}"], v[f"n_restated_{k}"] = (
                    int(r[0]),
                    float(r[1]),
                    int(r[2]),
                )
        else:
            raise ConfigError(f"F5 has no workload {workload!r}")
        return WorkloadResult(v, self._rows, self._bytes, "duckdb_profile")

    def teardown(self) -> None:
        """Close the connection and delete the store."""
        if self._con is not None:
            self._con.close()
            self._con = None
        shutil.rmtree(self.workdir, ignore_errors=True)


FORMATS: dict[str, Callable[[Path], Format]] = {"F5": F5Format}  # T-202/T-203 register F0-F4, F6


def env_info() -> dict[str, str]:
    """Machine / OS / library versions recorded with every result file."""
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": str(os.cpu_count()),
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "pyarrow": pa.__version__,
        "polars": pl.__version__,
        "numpy": np.__version__,
        "tidal": __version__,
    }


def _time(fn: Callable[[], Any]) -> tuple[float, float, Any]:  # Any: whatever fn returns
    c0, t0 = time.process_time(), time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, time.process_time() - c0, out


def _row(
    spec: DatasetSpec,
    fmt: Format,
    *,
    wl: str,
    cache: str,
    samples: list[tuple[float, float]],
    rows: int,
    at_rest: int,
    res: WorkloadResult | None,
) -> ResultRow:
    wall = np.array([s[0] for s in samples])
    p50 = float(np.percentile(wall, 50))
    return ResultRow(
        dataset_key=spec.key(), scale=spec.scale, seed=spec.seed, format_id=fmt.id, workload_id=wl,
        cache=cache,  # type: ignore[arg-type]  # "cold" | "warm" by construction
        runs=len(samples), wall_p50_s=p50, wall_p95_s=float(np.percentile(wall, 95)),
        cpu_s=float(np.median([s[1] for s in samples])), rows=rows,
        rows_per_s=rows / p50 if rows and p50 > 0 else None, bytes_at_rest=at_rest,
        bytes_read=None if res is None else res.bytes_read,
        bytes_read_source="n/a" if res is None else res.bytes_read_source,
        storage_price_key=fmt.storage_price_key,
    )  # fmt: skip


def run_benchmark(
    spec: DatasetSpec,
    format_ids: list[str],
    workload_ids: list[str],
    runs: int,
    out: Path,
    *,
    data_root: Path = DATA_ROOT,
    factories: Mapping[str, Callable[[Path], Format]] = FORMATS,
) -> list[ResultRow]:
    """Run every format x workload, check each result against the oracle, write ``out``."""
    ds = build_dataset(spec, data_root)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[ResultRow] = []
    tmp = Path(tempfile.mkdtemp(prefix="tidal-bench-"))
    try:
        for fid in format_ids:
            factory = factories[fid]
            if "W1" in workload_ids:
                rows.append(_run_w1(spec, ds, factory, tmp / f"{fid}-w1", runs))
            queries = [w for w in workload_ids if w != "W1"]
            if not queries:
                continue
            fmt = factory(tmp / fid)
            fmt.load(ds)
            at_rest = fmt.size_bytes()
            for wid in queries:
                rows += _run_query(spec, ds, fmt, wid, runs=runs, at_rest=at_rest)
            fmt.teardown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    env = env_info()
    write_results(out / "results.parquet", rows, env)
    summary = {
        "env": env,
        "spec": asdict(spec),
        "dataset": {"key": spec.key(), "content_hash": ds.content_hash, "rows": ds.rows},
        "results": [r.model_dump() for r in rows],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return rows


def _run_w1(
    spec: DatasetSpec, ds: Dataset, factory: Callable[[Path], Format], work: Path, runs: int
) -> ResultRow:
    sub = ds.select(list(ds.instruments[:1]))
    want = WORKLOADS["W1"].oracle(sub, WORKLOADS["W1"].params(sub))["rows"]
    samples: list[tuple[float, float]] = []
    for r in range(runs):
        fmt = factory(work / str(r))
        wall, cpu, stats = _time(lambda f=fmt: f.load(sub))  # type: ignore[misc]
        if stats.rows != want:
            raise OracleMismatchError(f"W1 {fmt.id}: wrote {stats.rows} rows, expected {want}")
        samples.append((wall, cpu))
        size = fmt.size_bytes()
        fmt.teardown()
    return _row(
        spec, fmt, wl="W1", cache="cold", samples=samples, rows=int(want), at_rest=size, res=None
    )


def _run_query(
    spec: DatasetSpec, ds: Dataset, fmt: Format, wid: str, *, runs: int, at_rest: int
) -> list[ResultRow]:
    wl = WORKLOADS[wid]
    p = wl.params(ds)
    want = wl.oracle(ds, p)

    def once() -> tuple[float, float, WorkloadResult]:
        wall, cpu, res = _time(lambda: fmt.run(wid, p))
        if not values_match(res.value, want):
            raise OracleMismatchError(f"{wid} {fmt.id}: {res.value} != oracle {want}")
        return wall, cpu, res

    cold = []
    for _ in range(runs):
        fmt.reopen()
        cold.append(once())
    once()  # untimed priming run
    warm = [once() for _ in range(runs)]
    return [
        _row(
            spec,
            fmt,
            wl=wid,
            cache=name,
            samples=[(a, b) for a, b, _ in s],
            rows=s[-1][2].rows_scanned or 0,
            at_rest=at_rest,
            res=s[-1][2],
        )
        for name, s in (("cold", cold), ("warm", warm))
    ]


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    if not os.environ.get("TIDAL_ENV"):
        raise ConfigError("TIDAL_ENV is required (local|dev|prod); it has no default")
    ap = argparse.ArgumentParser(prog="benchmarks.storage.run")
    ap.add_argument("--scale", default="S", choices=("S", "M", "L"))
    ap.add_argument("--formats", default="F5")
    ap.add_argument("--workloads", default="W1..W6")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2024, 12, 31))
    ap.add_argument("--instruments", type=int, default=None, help="cap on instruments")
    ap.add_argument("--data-root", type=Path, default=DATA_ROOT)
    a = ap.parse_args(argv)
    spec = DatasetSpec(a.scale, a.seed, a.start, a.end, a.instruments)
    res = run_benchmark(spec, parse_ids(a.formats, "F"), parse_ids(a.workloads, "W"), a.runs, a.out,
                        data_root=a.data_root)  # fmt: skip
    for r in res:
        print(
            f"{r.format_id} {r.workload_id} {r.cache:4} p50={r.wall_p50_s:.4f}s"
            f" p95={r.wall_p95_s:.4f}s cpu={r.cpu_s:.3f}s rows={r.rows}"
            f" read={r.bytes_read} rest={r.bytes_at_rest}"
        )


if __name__ == "__main__":
    main()
