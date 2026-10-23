"""Benchmark runner (SPEC-02 section 5); the formats live in ``benchmarks/storage/formats/``.

``uv run python -m benchmarks.storage.run --scale S --formats F5 --workloads W1..W6 --runs 5``
(plus ``--out DIR``; needs ``TIDAL_ENV``). ``--target azurite`` runs F3-F6 against the Azurite
account in ``TIDAL_STORAGE__CONNECTION_STRING`` (real request counts; the key is never printed);
``--formats F0..F5,F6-sweep`` expands the 3 x 3 F6 grid. Before a run, ``bench_*`` schemas and
``bench-*`` containers left by crashed runs are dropped when older than ``--sweep-min-age``.

Cold = a fresh connection per run (SQL additionally empties the buffer pool when it may; the OS
page cache is NOT dropped, so cold understates a truly cold read); warm = one untimed priming run,
then repeats on the same connection. W1 is a fresh store per run. The data source (reading the
cached dataset) is outside the W1 timer for every format alike. Formats plug in through
``FORMATS`` (F0-F2 SQL, F3-F6 files; F6 sweep ids too).
"""

import argparse
import json
import os
import platform
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import duckdb
import numpy as np
import polars as pl
import pyarrow as pa

from benchmarks.storage.formats import ALL_FORMATS, SQL_FORMATS, f6_ids, file_formats
from benchmarks.storage.formats.file_base import BlobTarget
from benchmarks.storage.formats.file_f5 import F6_DEFAULT, F5Format, rg_label
from benchmarks.storage.formats.sql_base import SqlTarget
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
from tidal.common.timeutil import SystemClock

if TYPE_CHECKING:
    from azure.storage.blob import BlobServiceClient

F6_DEFAULT_ID = f"F6-z{F6_DEFAULT[0]}-rg{rg_label(F6_DEFAULT[1])}"
_STALE_SCHEMA = re.compile(r"bench_[0-9a-f]{10}")
_STALE_CONTAINER = re.compile(r"bench-[a-z0-9-]+")


class OracleMismatchError(AssertionError):
    """A format returned a different aggregate than the independent polars oracle."""


__all__ = [
    "FORMATS",
    "F5Format",
    "OracleMismatchError",
    "env_info",
    "format_ids_from_arg",
    "main",
    "run_benchmark",
    "sweep_stale_containers",
    "sweep_stale_schemas",
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


def format_ids_from_arg(arg: str) -> list[str]:
    """``F0..F5,F6-sweep`` -> ids; the token ``F6-sweep`` expands the 3 x 3 F6 grid."""
    out: list[str] = []
    for part in arg.split(","):
        out += f6_ids() if part == "F6-sweep" else parse_ids(part, "F")
    # bare F6 is the F6-z9-rg1m grid point (its results carry that id): never run it twice
    return list(dict.fromkeys(F6_DEFAULT_ID if f == "F6" else f for f in out))


def _time(fn: Callable[[], Any]) -> tuple[float, float, Any]:  # Any: whatever fn returns
    c0, t0 = time.process_time(), time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, time.process_time() - c0, out


def _extras(fmt: Format, at_rest: int) -> dict[str, Any]:  # Any: ResultRow kwargs
    """Optional per-format measurements, read by duck typing (SQL / Parquet only)."""
    out: dict[str, Any] = {"bytes_billed": at_rest}  # Blob bills the stored bytes
    alloc = getattr(fmt, "allocated_bytes", None)
    if alloc is not None:
        out["bytes_billed"] = int(alloc())  # SQL: allocated (reserved) pages, see sql_base
    corr = getattr(fmt, "size_correction_bytes", None)
    if corr is not None and int(corr()) > 0:
        out["bytes_at_rest_adjusted"] = at_rest - int(corr())
    rg = getattr(fmt, "row_group_summary", None)
    if rg is not None and (s := rg())["files"]:
        out.update(row_groups_files=int(s["files"]), row_groups_total=int(s["total"]),
                   row_groups_max=int(s["max"]))  # fmt: skip
    return out


def _request_cols(fmt: Format) -> dict[str, Any]:  # Any: ResultRow kwargs
    """Real HTTP counts of the last ``io_profile`` (Blob mode only)."""
    r = getattr(fmt, "last_requests", None)
    if r is None:
        return {}
    return {"requests_get": r.get, "requests_head": r.head, "requests_list": r.list,
            "requests_other": r.other, "egress_bytes": r.egress_bytes}  # fmt: skip


def _row(
    spec: DatasetSpec,
    fmt: Format,
    *,
    wl: str,
    cache: str,
    samples: list[tuple[float, float]],
    rows: int | None,
    at_rest: int,
    res: WorkloadResult | None,
    target: str,
    extras: Mapping[str, Any],  # Any: ResultRow kwargs from _extras / _request_cols
    cold_mode: str = "n/a",
) -> ResultRow:
    wall = np.array([s[0] for s in samples])
    p50 = float(np.percentile(wall, 50))
    return ResultRow(
        dataset_key=spec.key(), scale=spec.scale, seed=spec.seed, format_id=fmt.id, workload_id=wl,
        target="sql_server" if fmt.id in SQL_FORMATS else target,
        cache=cache,  # type: ignore[arg-type]  # "cold" | "warm" by construction
        runs=len(samples), wall_p50_s=p50, wall_p95_s=float(np.percentile(wall, 95)),
        cpu_s=float(np.median([s[1] for s in samples])), rows=rows,
        rows_per_s=rows / p50 if rows and p50 > 0 else None, bytes_at_rest=at_rest,
        bytes_read=None if res is None else res.bytes_read,
        bytes_read_source="n/a" if res is None else res.bytes_read_source,
        storage_price_key=fmt.storage_price_key, cold_mode=cold_mode, **extras,
    )  # fmt: skip


def sweep_stale_schemas(
    target: SqlTarget, *, min_age_min: int = 60, only: Collection[str] | None = None
) -> list[str]:
    """Drop ``bench_<10 hex>`` schemas (tables first) whose newest object is older than the age.

    A crashed run leaves its throwaway schema behind; a live run's objects are younger than
    ``min_age_min`` and are left alone. Only names matching the generated pattern are touched
    (so the identifiers interpolated below are safe); ``only`` restricts the sweep to those names.
    Returns the dropped schema names.
    """
    con = target.connect()
    dropped: list[str] = []
    try:
        rows = con.execute(
            "SELECT s.name, MAX(o.create_date) FROM sys.schemas s LEFT JOIN sys.objects o"
            " ON o.schema_id = s.schema_id WHERE s.name LIKE 'bench[_]%' GROUP BY s.name"
        ).fetchall()
        now = con.execute("SELECT SYSDATETIME()").fetchone()[0]
        for name, newest in rows:
            if not _STALE_SCHEMA.fullmatch(name) or (only is not None and name not in only):
                continue
            if newest is not None and now - newest < timedelta(minutes=min_age_min):
                continue
            tables = con.execute(
                "SELECT t.name FROM sys.tables t WHERE t.schema_id = SCHEMA_ID(?)", name
            ).fetchall()
            for (tbl,) in tables:
                con.execute(f"DROP TABLE [{name}].[{tbl.replace(']', ']]')}]")
            con.execute(f"DROP SCHEMA [{name}]")
            dropped.append(name)
    finally:
        con.close()
    return dropped


_EMULATOR_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "azurite"})


def sweep_stale_containers(
    service: "BlobServiceClient",
    *,
    min_age_min: int = 60,
    now: datetime | None = None,
    prefix: str = "bench-",
) -> list[str]:
    """Delete ``bench-*`` containers (starting with ``prefix``) older than ``min_age_min``.

    Refuses to run against anything but a local emulator: a misconfigured connection string
    must never let a benchmark delete containers in a real storage account.
    """
    host = urlparse(service.url).hostname or ""
    if host not in _EMULATOR_HOSTS and service.account_name != "devstoreaccount1":
        raise ConfigError(f"container sweep only runs against Azurite, not {host!r}")
    cutoff = (now or SystemClock().now()) - timedelta(minutes=min_age_min)
    dropped: list[str] = []
    for c in service.list_containers(name_starts_with=prefix):
        modified = c.last_modified
        if not _STALE_CONTAINER.fullmatch(c.name) or modified > cutoff:
            continue
        service.delete_container(c.name)
        dropped.append(c.name)
    return dropped


def run_benchmark(
    spec: DatasetSpec,
    format_ids: list[str],
    workload_ids: list[str],
    runs: int,
    out: Path,
    *,
    data_root: Path = DATA_ROOT,
    factories: Mapping[str, Callable[[Path], Format]] = FORMATS,
    target: str = "local",
    w1_runs: int | None = None,
    parallel_load: bool = False,
    scale_label: str | None = None,
) -> list[ResultRow]:
    """Run every format x workload, check each result against the oracle, write ``out``.

    ``target`` labels file-format rows ("local" or "azurite"); ``w1_runs`` overrides the number
    of fresh-store W1 loads (SQL loads are slow). ``parallel_load`` runs all W1s first (one at a
    time, nothing else running), then loads every format's query store concurrently in threads
    (an untimed phase), then runs the timed queries one format at a time. Results are flushed to
    ``out`` after every format so a crash keeps what finished. ``scale_label`` renames the
    ``scale`` column of the rows (a slice of S is labelled e.g. "S-1y"; ``dataset_key`` keeps the
    real dataset identity).
    """
    ds = build_dataset(spec, data_root)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[ResultRow] = []
    flush = partial(_flush, out, spec, ds, label=scale_label)
    tmp = Path(tempfile.mkdtemp(prefix="tidal-bench-"))
    try:
        queries = [w for w in workload_ids if w != "W1"]
        w1_n = w1_runs or runs
        if "W1" in workload_ids and (parallel_load or not queries):
            for fid in format_ids:
                rows.append(
                    _run_w1(spec, ds, factories[fid], tmp / f"{fid}-w1", runs=w1_n, target=target)
                )
                flush(rows)
        if parallel_load:
            _run_loaded_together(
                spec,
                ds,
                format_ids,
                queries,
                runs=runs,
                factories=factories,
                tmp=tmp,
                target=target,
                flush=flush,
                rows=rows,
            )
        else:
            for fid in format_ids:
                factory = factories[fid]
                if "W1" in workload_ids and queries:
                    rows.append(
                        _run_w1(spec, ds, factory, tmp / f"{fid}-w1", runs=w1_n, target=target)
                    )
                if not queries:
                    continue
                fmt = factory(tmp / fid)
                try:  # a failed load/query must not leak tables or containers
                    fmt.load(ds)
                    at_rest = fmt.size_bytes()
                    for wid in queries:
                        rows += _run_query(
                            spec, ds, fmt, wid, runs=runs, at_rest=at_rest, target=target
                        )
                finally:
                    fmt.teardown()
                flush(rows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return flush(rows)


def _flush(
    out: Path, spec: DatasetSpec, ds: Dataset, rows: list[ResultRow], *, label: str | None = None
) -> list[ResultRow]:
    """Write ``results.parquet`` and ``summary.json`` so far; returns the rows (relabelled)."""
    if label:
        rows = [r.model_copy(update={"scale": label}) for r in rows]
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


def _run_loaded_together(
    spec: DatasetSpec,
    ds: Dataset,
    format_ids: list[str],
    queries: list[str],
    *,
    runs: int,
    factories: Mapping[str, Callable[[Path], Format]],
    tmp: Path,
    target: str,
    flush: Callable[[list[ResultRow]], object],
    rows: list[ResultRow],
) -> None:
    """Load all formats' stores concurrently (untimed), then time queries one format at a time.

    Appends to ``rows`` in place so every flush includes the W1 rows from before.
    """
    fmts: dict[str, Format] = {}
    try:
        if queries:
            for fid in format_ids:
                fmts[fid] = factories[fid](tmp / fid)
            with ThreadPoolExecutor(max_workers=len(fmts)) as ex:
                futs = [ex.submit(f.load, ds) for f in fmts.values()]
            for fut in futs:
                fut.result()  # the first load error propagates; the pool has already drained
            for fmt in fmts.values():
                at_rest = fmt.size_bytes()
                for wid in queries:
                    rows += _run_query(
                        spec, ds, fmt, wid, runs=runs, at_rest=at_rest, target=target
                    )
                flush(rows)
    finally:
        for fmt in fmts.values():
            fmt.teardown()


def _run_w1(
    spec: DatasetSpec,
    ds: Dataset,
    factory: Callable[[Path], Format],
    work: Path,
    *,
    runs: int,
    target: str,
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
            extras = _extras(fmt, size)  # read before teardown (SQL needs its schema)
        finally:
            fmt.teardown()
    return _row(
        spec, fmt, wl="W1", cache="n/a", samples=samples, rows=int(want), at_rest=size, res=None,
        target=target, extras=extras,
    )  # fmt: skip


def _run_query(
    spec: DatasetSpec, ds: Dataset, fmt: Format, wid: str, *, runs: int, at_rest: int, target: str
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
    cold_modes: set[str] = set()
    for _ in range(runs):
        fmt.reopen()
        cold_modes.add(getattr(fmt, "cold_mode", "fresh_connection"))  # SQL sets it per reopen
        cold.append(once())
    once()  # untimed priming run
    warm = [once() for _ in range(runs)]
    io = fmt.io_profile(wid, p)  # separate untimed pass: profiling never inflates wall/CPU
    if not values_match(io.value, want):
        raise OracleMismatchError(f"{wid} {fmt.id}: profiled {io.value} != oracle {want}")
    cold_mode = cold_modes.pop() if len(cold_modes) == 1 else "mixed"
    extras = _extras(fmt, at_rest) | _request_cols(fmt)
    return [
        _row(
            spec,
            fmt,
            wl=wid,
            cache=name,
            samples=[(a, b) for a, b, _ in s],
            rows=io.rows_scanned,  # None for SQL (not reported): shown as n/a, never 0
            at_rest=at_rest,
            res=io,
            target=target,
            extras=extras,
            cold_mode=cold_mode if name == "cold" else "n/a",
        )
        for name, s in (("cold", cold), ("warm", warm))
    ]


def _blob_target() -> BlobTarget:
    """Azurite account from ``TIDAL_STORAGE__CONNECTION_STRING`` (never printed or logged)."""
    from azure.storage.blob import BlobServiceClient  # noqa: PLC0415 - Azurite mode only

    cs = os.environ.get("TIDAL_STORAGE__CONNECTION_STRING")
    if not cs:
        raise ConfigError("--target azurite needs TIDAL_STORAGE__CONNECTION_STRING (see .env)")
    return BlobTarget(BlobServiceClient.from_connection_string(cs), cs)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    if not os.environ.get("TIDAL_ENV"):
        raise ConfigError("TIDAL_ENV is required (local|dev|prod); it has no default")
    ap = argparse.ArgumentParser(prog="benchmarks.storage.run")
    ap.add_argument("--scale", default="S", choices=("S", "M", "L"))
    ap.add_argument("--formats", default="F5", help="e.g. F0..F5,F6-sweep or F5,F6-z3-rg128k")
    ap.add_argument("--workloads", default="W1..W6")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--w1-runs", type=int, default=None, help="W1 loads per format (default: runs)")
    ap.add_argument("--target", default="local", choices=("local", "azurite"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="default: from scale")
    ap.add_argument(
        "--end", type=date.fromisoformat, default=None, help="default: TIDAL_SYNTH__END"
    )
    ap.add_argument("--instruments", type=int, default=None, help="cap on instruments")
    ap.add_argument("--data-root", type=Path, default=DATA_ROOT)
    ap.add_argument(
        "--parallel-load", action="store_true", help="W1s first, then load all formats concurrently"
    )
    ap.add_argument("--label", default=None, help='scale column in the results, e.g. "S-1y"')
    ap.add_argument("--no-sweep", action="store_true", help="skip the stale bench_*/bench-* sweep")
    ap.add_argument("--sweep-min-age", type=int, default=60, help="minutes; younger is kept")
    a = ap.parse_args(argv)
    start, end = default_range(a.scale, a.end)
    spec = DatasetSpec(a.scale, a.seed, a.start or start, end, a.instruments)
    fids = format_ids_from_arg(a.formats)
    blob = _blob_target() if a.target == "azurite" else None
    factories: dict[str, Callable[[Path], Format]] = {**SQL_FORMATS, **file_formats(blob)}
    if blob is not None and any(f in SQL_FORMATS for f in fids):
        print("note: SQL formats always run against SQL Server; --target applies to F3-F6")
    if not a.no_sweep:
        if any(f in SQL_FORMATS for f in fids):
            dropped = sweep_stale_schemas(SqlTarget.from_env(), min_age_min=a.sweep_min_age)
            print(f"swept {len(dropped)} stale bench_* schemas")
        if blob is not None:
            gone = sweep_stale_containers(blob.service, min_age_min=a.sweep_min_age)
            print(f"swept {len(gone)} stale bench-* containers")
    res = run_benchmark(
        spec, fids, parse_ids(a.workloads, "W"), a.runs, a.out, data_root=a.data_root,
        factories=factories, target=a.target, w1_runs=a.w1_runs,
        parallel_load=a.parallel_load, scale_label=a.label,
    )  # fmt: skip
    for r in res:
        print(
            f"{r.format_id} {r.workload_id} {r.cache:4} p50={r.wall_p50_s:.4f}s"
            f" p95={r.wall_p95_s:.4f}s cpu={r.cpu_s:.3f}s rows={r.rows}"
            f" read={r.bytes_read} rest={r.bytes_at_rest}"
        )


if __name__ == "__main__":
    main()
