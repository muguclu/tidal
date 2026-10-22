"""Benchmark runner (SPEC-02 section 5); the formats live in ``benchmarks/storage/formats/``.

``uv run python -m benchmarks.storage.run --scale S --formats F5 --workloads W1..W6 --runs 5``
(plus ``--out DIR``; needs ``TIDAL_ENV``).

Cold = a fresh connection per run (the OS page cache is NOT dropped, so cold understates a truly
cold read); warm = one untimed priming run, then repeats on the same connection. W1 is a fresh
store per run. The data source (reading the cached dataset) is part of W1 wall time for every
format alike. Formats plug in through ``FORMATS`` (F0-F2 SQL, F3-F6 files; F6 sweep ids too).
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

from benchmarks.storage.formats import ALL_FORMATS
from benchmarks.storage.formats.file_f5 import F5Format
from benchmarks.storage.results_schema import ResultRow, write_results
from benchmarks.storage.workloads import (
    DATA_ROOT,
    WORKLOADS,
    Dataset,
    DatasetSpec,
    Format,
    WorkloadResult,
    build_dataset,
    default_range,
    parse_ids,
    values_match,
)
from tidal import __version__
from tidal.common.errors import ConfigError


class OracleMismatchError(AssertionError):
    """A format returned a different aggregate than the independent polars oracle."""


__all__ = [
    "FORMATS",
    "F5Format",
    "OracleMismatchError",
    "env_info",
    "main",
    "run_benchmark",
]  # F5Format: moved, kept importable

FORMATS: dict[str, Callable[[Path], Format]] = dict(ALL_FORMATS)


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
            try:  # a failed load/query must not leak tables or containers
                fmt.load(ds)
                at_rest = fmt.size_bytes()
                for wid in queries:
                    rows += _run_query(spec, ds, fmt, wid, runs=runs, at_rest=at_rest)
            finally:
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
    sub = ds.select(list(ds.instruments[:1])).preloaded()  # source I/O stays outside the timer
    want = WORKLOADS["W1"].oracle(sub, WORKLOADS["W1"].params(sub))["rows"]
    samples: list[tuple[float, float]] = []
    for r in range(runs):
        fmt = factory(work / str(r))
        try:
            wall, cpu, stats = _time(lambda f=fmt: f.load(sub))  # type: ignore[misc]
            if stats.rows != want:
                raise OracleMismatchError(f"W1 {fmt.id}: wrote {stats.rows} rows, expected {want}")
            samples.append((wall, cpu))
            size = fmt.size_bytes()
        finally:
            fmt.teardown()
    return _row(
        spec, fmt, wl="W1", cache="n/a", samples=samples, rows=int(want), at_rest=size, res=None
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
    io = fmt.io_profile(wid, p)  # separate untimed pass: profiling never inflates wall/CPU
    if not values_match(io.value, want):
        raise OracleMismatchError(f"{wid} {fmt.id}: profiled {io.value} != oracle {want}")
    return [
        _row(
            spec,
            fmt,
            wl=wid,
            cache=name,
            samples=[(a, b) for a, b, _ in s],
            rows=io.rows_scanned or 0,
            at_rest=at_rest,
            res=io,
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
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="default: from scale")
    ap.add_argument(
        "--end", type=date.fromisoformat, default=None, help="default: TIDAL_SYNTH__END"
    )
    ap.add_argument("--instruments", type=int, default=None, help="cap on instruments")
    ap.add_argument("--data-root", type=Path, default=DATA_ROOT)
    a = ap.parse_args(argv)
    start, end = default_range(a.scale, a.end)
    spec = DatasetSpec(a.scale, a.seed, a.start or start, end, a.instruments)
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
