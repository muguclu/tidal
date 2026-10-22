"""Render benchmark results as the SPEC-02 report: Markdown + matplotlib charts, MODELLED EUR.

``uv run python -m benchmarks.storage.report --results out/S-local out/S-azurite out/S-sql \
  --repro out/S-local out/S-repro --prices docs/finops/prices.yaml \
  --out docs/finops/results/storage-benchmark-2026-10-22.md --img docs/finops/results/img``

Wall, CPU, bytes and request counts are MEASURED (results files). Every EUR figure is MODELLED =
quantity x unit price from the pinned ``prices.yaml``; no price is hard-coded. Compute is only
modelled for SQL (1 vCore x wall time at the serverless rate, labelled); Container Apps compute
for the file formats stays out because its per-second meters are ``precision_limited`` (C5), so it
is reported MEASURED from Cost Management in the Azure run, never modelled here.
"""

import argparse
import json
import math
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pyarrow as pa
import pyarrow.parquet as pq

from benchmarks.storage.results_schema import ResultRow, bytes_read_class, read_results
from tidal.contracts.schemas import pyarrow_write_kwargs
from tidal.finops.fetch_prices import PriceBook, load_prices
from tidal.storage import parquet_writer as pw

GIB: Final = 1024**3
MIB: Final = 1024**2
READ_BLOCK: Final = 4 * 1024**2  # assumed bytes per blob read operation when no real count exists
READ_KEY: Final = "blob_hot_read_10k"
LIST_KEY: Final = "blob_hot_write_10k"  # List Blobs is billed in the write/list/create class
OTHER_KEY: Final = "blob_hot_other_10k"
EGRESS_KEY: Final = "egress_first_tier_gb"
SQL_VCORE_KEY: Final = "sql_gp_serverless_vcore_hour"
SIZE_TOL: Final = 0.01  # SPEC-02 section 6
TIME_TOL: Final = 0.15
NOISE_FLOOR_S: Final = 0.1  # timings below this are dominated by process noise; also reported alone
QUERY_WLS: Final = ("W2", "W3", "W4", "W5", "W6")
_FORMAT_ORDER = re.compile(r"F(\d+)(?:-z(\d+)-rg(\d+)(k|m))?")
_ADJ: Final = " (adjusted, no ts_sub)"
_RAW: Final = " (raw)"


# --- price model (pure) --------------------------------------------------------------------------
def eur_per_month_at_rest(bytes_at_rest: int, prices: PriceBook, key: str) -> float:
    """MODELLED: ``bytes / 2^30 x unitPrice(key)`` (a ``1 GB/Month`` meter, billed per GiB)."""
    return bytes_at_rest / GIB * prices.unit_price(key)


def eur_per_1k_queries(
    bytes_read: int | None, prices: PriceBook, *, internet_egress: bool = False
) -> float | None:
    """MODELLED lower bound without request counts: ``1000 x ceil(bytes / 4 MiB) / 10,000 x read``.

    The 4 MiB-per-GET operation count is a LOWER BOUND (real readers issue more, smaller range
    requests; use :func:`eur_per_1k_from_requests` where Azurite counted them). Egress is 0 by
    default because SPEC-02 section 5 queries from Container Apps in the same region;
    ``internet_egress=True`` adds ``bytes / 2^30 x egress_first_tier_gb``. ``None`` when the
    format reports no bytes read.
    """
    if bytes_read is None:
        return None
    per_query = math.ceil(bytes_read / READ_BLOCK) / 10_000 * prices.unit_price(READ_KEY)
    if internet_egress:
        per_query += bytes_read / GIB * prices.unit_price(EGRESS_KEY)
    return 1000 * per_query


def eur_per_1k_from_requests(r: ResultRow, prices: PriceBook) -> float | None:
    """MODELLED from REAL request counts: GET+HEAD as reads, LIST as list class, rest as other.

    In-region egress is free, so there is no egress term. ``None`` without request counts.
    """
    if r.requests_get is None:
        return None
    read = (r.requests_get + (r.requests_head or 0)) * prices.unit_price(READ_KEY)
    lst = (r.requests_list or 0) * prices.unit_price(LIST_KEY)
    other = (r.requests_other or 0) * prices.unit_price(OTHER_KEY)
    return 1000 * (read + lst + other) / 10_000


def eur_per_1k_sql_compute(wall_s: float, prices: PriceBook) -> float:
    """MODELLED: 1 vCore busy for the query's wall time at the serverless rate, x 1000."""
    return 1000 * wall_s * prices.unit_price(SQL_VCORE_KEY) / 3600


# --- selection helpers ---------------------------------------------------------------------------
def format_sort_key(fid: str) -> tuple[int, int, int, int]:
    """F0..F6 in order, F6 sweep points after the bare F6 by level then row group."""
    m = _FORMAT_ORDER.fullmatch(fid)
    if not m:
        return (99, 0, 0, 0)
    rg = int(m[3]) * (1000 if m[4] == "k" else 1_000_000) if m[3] else 0
    return (int(m[1]), 1 if m[2] else 0, int(m[2] or 0), rg)


def _find(
    rows: Iterable[ResultRow],
    scale: str,
    fid: str,
    wl: str,
    cache: str,
    *,
    target: str | None = None,
) -> ResultRow | None:
    for r in rows:
        if (r.scale, r.format_id, r.workload_id, r.cache) == (scale, fid, wl, cache) and (
            target is None or r.target == target
        ):
            return r
    return None


def _main_row(
    rows: Sequence[ResultRow], scale: str, fid: str, wl: str, cache: str
) -> ResultRow | None:
    """The timing row of a cell: the local-file or SQL one, never the HTTP-proxied Azurite one."""
    return _find(rows, scale, fid, wl, cache, target="local") or _find(
        rows, scale, fid, wl, cache, target="sql_server"
    )


def _size_row(rows: Sequence[ResultRow], scale: str, fid: str) -> ResultRow | None:
    """Any query row of the format (sizes do not depend on the target); Azurite as a fallback."""
    for wl in QUERY_WLS:
        r = _main_row(rows, scale, fid, wl, "warm") or _find(
            rows, scale, fid, wl, "warm", target="azurite"
        )
        if r is not None:
            return r
    return None


def _scales(rows: Sequence[ResultRow]) -> list[str]:
    return sorted({r.scale for r in rows})


def _fids(rows: Sequence[ResultRow], scale: str, *, sweep: bool) -> list[str]:
    ids = {r.format_id for r in rows if r.scale == scale}
    return sorted((f for f in ids if ("-z" in f) == sweep), key=format_sort_key)


def _all_fids(rows: Sequence[ResultRow], scale: str) -> list[str]:
    return _fids(rows, scale, sweep=False) + _fids(rows, scale, sweep=True)


def _bytes(n: float | None) -> str:
    if n is None:
        return "n/a"
    for unit, size in (("GiB", GIB), ("MiB", MIB), ("KiB", 1024)):
        if abs(n) >= size:
            return f"{n / size:,.2f} {unit}"
    return f"{n:,.0f} B"


def _sec(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:.3f}" if x >= 0.01 else f"{x * 1000:.1f} ms"


def _eur(x: float | None, digits: int = 4) -> str:
    return "n/a" if x is None else f"{x:,.{digits}f}"


def _ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else a / b


def _x(v: float | None) -> str:
    return "n/a" if v is None else f"{v:,.1f}x"


def _short(label: str) -> str:
    return label.replace(_ADJ, " adj").replace(_RAW, " raw")


def _table(head: Sequence[str], body: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines) + "\n"


# --- run sets ------------------------------------------------------------------------------------
@dataclass
class RunSet:
    """All rows from one or more result directories plus per-scale dataset row counts."""

    rows: list[ResultRow] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    ds_rows: dict[str, int] = field(default_factory=dict)  # scale -> base ticks in the dataset
    not_run: list[str] = field(default_factory=list)


def load_runs(dirs: Sequence[Path]) -> RunSet:
    """Merge ``results.parquet`` (+ ``summary.json`` dataset sizes) of several runs."""
    rs = RunSet()
    seen: set[tuple[str, ...]] = set()
    for d in dirs:
        rows, env = read_results(d / "results.parquet")
        rs.env = rs.env or env
        for r in rows:
            k = (r.scale, r.target, r.format_id, r.workload_id, r.cache)
            if k in seen:
                raise ValueError(f"duplicate result {k} while merging {d}")
            seen.add(k)
        rs.rows += rows
        s = d / "summary.json"
        if s.exists() and rows:
            ds = json.loads(s.read_text(encoding="utf-8"))["dataset"]["rows"]
            rs.ds_rows[rows[0].scale] = int(sum(ds.values()))
    return rs


# --- size and EUR --------------------------------------------------------------------------------
@dataclass(frozen=True)
class SizeLine:
    """One at-rest line: ``label`` is the format id, with F0 split into raw and adjusted."""

    label: str
    fid: str
    used: int
    billed: int
    price_key: str


def size_lines(rows: Sequence[ResultRow], scale: str) -> list[SizeLine]:
    """At-rest lines per format; F0 appears twice (raw, and minus the ts_sub byte per row)."""
    out: list[SizeLine] = []
    for fid in _all_fids(rows, scale):
        r = _size_row(rows, scale, fid)
        if r is None:
            continue
        billed = r.bytes_billed if r.bytes_billed is not None else r.bytes_at_rest
        key = r.storage_price_key
        if r.bytes_at_rest_adjusted is not None:
            cut = r.bytes_at_rest - r.bytes_at_rest_adjusted
            out.append(SizeLine(fid + _RAW, fid, r.bytes_at_rest, billed, key))
            out.append(SizeLine(fid + _ADJ, fid, r.bytes_at_rest_adjusted, billed - cut, key))
        else:
            out.append(SizeLine(fid, fid, r.bytes_at_rest, billed, key))
    return out


def _used(ln: SizeLine | None) -> int | None:
    return None if ln is None else ln.used


def _line_eur(ln: SizeLine | None, prices: PriceBook) -> float | None:
    return None if ln is None else eur_per_month_at_rest(ln.billed, prices, ln.price_key)


def headline_ratios(
    rows: Sequence[ResultRow], prices: PriceBook, scale: str
) -> dict[str, float | None]:
    """F0..F4 vs F5 size and EUR ratios (F0 raw and adjusted); ``None`` where a side is missing."""
    by = {ln.label: ln for ln in size_lines(rows, scale)}
    f5 = by.get("F5")
    out: dict[str, float | None] = {}
    for tag, key in (("raw", "F0" + _RAW), ("adj", "F0" + _ADJ)):
        f0 = by.get(key)
        out[f"size_f0_{tag}_over_f5"] = _ratio(_used(f0), _used(f5))
        out[f"eur_f0_{tag}_over_f5"] = _ratio(_line_eur(f0, prices), _line_eur(f5, prices))
    for other in ("F1", "F2", "F3", "F4"):
        ln = by.get(other)
        out[f"size_{other.lower()}_over_f5"] = _ratio(_used(ln), _used(f5))
        out[f"eur_{other.lower()}_over_f5"] = _ratio(_line_eur(ln, prices), _line_eur(f5, prices))
    return out


def render_size_table(
    rows: Sequence[ResultRow], prices: PriceBook, scale: str, ds_rows: int | None
) -> str:
    """Bytes at rest, bytes per tick, ratio to F5, and EUR/month on the billing basis."""
    lines = size_lines(rows, scale)
    f5 = next((ln for ln in lines if ln.label == "F5"), None)
    f5_eur = _line_eur(f5, prices)
    body = []
    for ln in lines:
        eur = eur_per_month_at_rest(ln.billed, prices, ln.price_key)
        per_tick = f"{ln.used / ds_rows:.2f}" if ds_rows else "n/a"
        body.append([
            ln.label, _bytes(ln.used), per_tick, _x(_ratio(ln.used, _used(f5))),
            _bytes(ln.billed), f"{eur:.4f}", _x(_ratio(eur, f5_eur)),
        ])  # fmt: skip
    head = ["Format", "Bytes at rest (used)", "B/tick", "vs F5 (size)", "Billed basis"]
    return _table([*head, "EUR/month (MODELLED)", "vs F5 (EUR)"], body)


def render_w1_table(rows: Sequence[ResultRow], scale: str) -> str:
    """W1 bulk-write wall time and throughput (one instrument, fresh store per run)."""
    body = []
    for fid in _all_fids(rows, scale):
        r = _main_row(rows, scale, fid, "W1", "n/a")
        if r is None:
            continue
        rate = "n/a" if r.rows_per_s is None else f"{r.rows_per_s:,.0f}"
        body.append([
            fid, str(r.runs), _sec(r.wall_p50_s), _sec(r.wall_p95_s), rate, f"{r.cpu_s:.1f}",
            _bytes(r.bytes_at_rest),
        ])  # fmt: skip
    head = ["Format", "Runs", "p50 s", "p95 s", "rows/s", "CPU s (client)"]
    return _table([*head, "Size of the 1-instrument store"], body)


def render_query_table(rows: Sequence[ResultRow], scale: str, cache: str) -> str:
    """p50 seconds per format x W2..W6 for one cache state."""
    body = []
    for fid in _fids(rows, scale, sweep=False):
        cells = []
        for wl in QUERY_WLS:
            r = _main_row(rows, scale, fid, wl, cache)
            cells.append("n/a" if r is None else _sec(r.wall_p50_s))
        if any(c != "n/a" for c in cells):
            body.append([fid, *cells])
    return _table(["Format", *QUERY_WLS], body)


def render_cold_modes(rows: Sequence[ResultRow], scale: str) -> str:
    """Which 'cold' each format actually got (SQL can empty its buffer pool, files cannot)."""
    modes: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.scale == scale and r.cache == "cold" and r.target != "azurite":
            modes[r.format_id].add(r.cold_mode)
    ordered = sorted(modes.items(), key=lambda kv: format_sort_key(kv[0]))
    return _table(["Format", "cold_mode"], [[f, ", ".join(sorted(m))] for f, m in ordered])


def render_io_table(rows: Sequence[ResultRow], scale: str) -> str:
    """Bytes read per workload with the source class spelled out (classes are not comparable)."""
    body = []
    for fid in _fids(rows, scale, sweep=False):
        for wl in QUERY_WLS:
            r = _main_row(rows, scale, fid, wl, "warm")
            if r is None:
                continue
            frac = _ratio(r.bytes_read, r.bytes_at_rest)
            body.append([
                fid, wl, _bytes(r.bytes_read), bytes_read_class(r.bytes_read_source),
                "n/a" if frac is None else f"{100 * frac:.1f} %",
                "n/a" if r.rows is None else f"{r.rows:,}",
            ])  # fmt: skip
    head = ["Format", "Workload", "Bytes read", "Source class", "of bytes at rest"]
    return _table([*head, "Rows scanned"], body)


def render_requests_table(rows: Sequence[ResultRow], prices: PriceBook, scale: str) -> str:
    """REAL Azurite HTTP request counts and egress per query, and the MODELLED transaction EUR."""
    body = []
    for fid in _fids(rows, scale, sweep=False):
        for wl in QUERY_WLS:
            r = _find(rows, scale, fid, wl, "warm", target="azurite")
            if r is None or r.requests_get is None:
                continue
            body.append([
                fid, wl, str(r.requests_get), str(r.requests_head), str(r.requests_list),
                str(r.requests_other), _bytes(r.egress_bytes),
                _eur(eur_per_1k_from_requests(r, prices), 6),
            ])  # fmt: skip
    head = ["Format", "Workload", "GET", "HEAD", "LIST", "other", "Egress"]
    return _table([*head, "EUR/1k queries, transactions (MODELLED)"], body)


def render_eur_query_table(rows: Sequence[ResultRow], prices: PriceBook, scale: str) -> str:
    """EUR per 1,000 queries: Blob transactions (files) or modelled 1-vCore compute (SQL)."""
    body = []
    for fid in _fids(rows, scale, sweep=False):
        for wl in QUERY_WLS:
            r = _main_row(rows, scale, fid, wl, "warm")
            if r is None:
                continue
            if r.target == "sql_server":
                compute = _eur(eur_per_1k_sql_compute(r.wall_p50_s, prices), 4)
                body.append([fid, wl, "n/a (no per-operation meter)", compute, "n/a"])
                continue
            az = _find(rows, scale, fid, wl, "warm", target="azurite")
            real = None if az is None else eur_per_1k_from_requests(az, prices)
            lower = eur_per_1k_queries(r.bytes_read, prices)
            body.append([fid, wl, _eur(real, 6), "excluded (C5)", _eur(lower, 6)])
    head = ["Format", "Workload", "Transactions, REAL counts", "Compute (SQL: 1 vCore x wall)"]
    return _table([*head, "Transactions, 4 MiB-GET lower bound"], body)


def render_rowgroup_table(rows: Sequence[ResultRow], scale: str) -> str:
    """Parquet files and row groups per format (from the loaded dataset)."""
    body = []
    for fid in _all_fids(rows, scale):
        r = _size_row(rows, scale, fid)
        if r is None or not r.row_groups_files:
            continue
        total, files = r.row_groups_total or 0, r.row_groups_files
        body.append([fid, str(files), str(total), f"{total / files:.2f}", str(r.row_groups_max)])
    return _table(["Format", "Files", "Row groups", "Mean/file", "Max/file"], body)


def render_sweep_table(rows: Sequence[ResultRow], scale: str) -> str:
    """F6 zstd-level x row-group sweep: size, W1 throughput, W2/W3 warm p50."""
    body = []
    for fid in _fids(rows, scale, sweep=True):
        sz = _size_row(rows, scale, fid)
        if sz is None:
            continue
        w1 = _main_row(rows, scale, fid, "W1", "n/a")
        w2 = _main_row(rows, scale, fid, "W2", "warm")
        w3 = _main_row(rows, scale, fid, "W3", "warm")
        rate = "n/a" if w1 is None or w1.rows_per_s is None else f"{w1.rows_per_s:,.0f}"
        body.append([
            fid, _bytes(sz.bytes_at_rest), rate, _sec(w2.wall_p50_s if w2 else None),
            _sec(w3.wall_p50_s if w3 else None),
        ])  # fmt: skip
    return _table(["Variant", "Bytes at rest", "W1 rows/s", "W2 warm p50", "W3 warm p50"], body)


def skip_table(rows: Sequence[ResultRow], scale: str) -> str:
    """Bytes read as a fraction of bytes at rest per format for the selective workloads."""
    body = []
    for fid in ("F3", "F4", "F5", "F6", "F0", "F1", "F2"):
        cells = []
        for wl in ("W3", "W4", "W2"):
            r = _main_row(rows, scale, fid, wl, "warm")
            frac = None if r is None else _ratio(r.bytes_read, r.bytes_at_rest)
            cells.append("n/a" if frac is None else f"{100 * frac:.2f} %")
        if any(c != "n/a" for c in cells):
            body.append([fid, *cells])
    return _table(["Format", "W3 (one month)", "W4 (last price)", "W2 (full scan)"], body)


# --- reproducibility -----------------------------------------------------------------------------
def compare_runs(a: Sequence[ResultRow], b: Sequence[ResultRow]) -> dict[str, Any]:
    """Run A vs run B of the same scale+seed: size and p50 deltas (relative to A).

    Returns ``cells`` (per format/workload/cache), the worst size delta and how many timings
    miss the 15 % tolerance, for all cells and for cells with p50 >= ``NOISE_FLOOR_S``.
    """
    idx = {(r.scale, r.target, r.format_id, r.workload_id, r.cache): r for r in b}
    cells: list[dict[str, Any]] = []  # Any: str labels and float deltas
    for ra in a:
        rb = idx.get((ra.scale, ra.target, ra.format_id, ra.workload_id, ra.cache))
        if rb is None:
            continue
        cells.append({
            "format": ra.format_id, "workload": ra.workload_id, "cache": ra.cache,
            "size_delta": abs(rb.bytes_at_rest - ra.bytes_at_rest) / ra.bytes_at_rest,
            "time_delta": abs(rb.wall_p50_s - ra.wall_p50_s) / ra.wall_p50_s,
            "p50_a": ra.wall_p50_s, "p50_b": rb.wall_p50_s,
        })  # fmt: skip
    big = [c for c in cells if c["p50_a"] >= NOISE_FLOOR_S]
    return {
        "cells": cells,
        "max_size_delta": max((c["size_delta"] for c in cells), default=0.0),
        "n": len(cells),
        "n_time_fail": sum(c["time_delta"] > TIME_TOL for c in cells),
        "n_big": len(big),
        "n_big_time_fail": sum(c["time_delta"] > TIME_TOL for c in big),
        "max_time_delta_big": max((c["time_delta"] for c in big), default=0.0),
    }


def render_repro(cmp: dict[str, Any]) -> str:
    """The reproducibility section; states what was measured against the SPEC-02 tolerances."""
    worst = sorted(cmp["cells"], key=lambda c: -c["time_delta"])[:8]
    ok = "PASS" if cmp["max_size_delta"] <= SIZE_TOL else "FAIL"
    floor_ms = NOISE_FLOOR_S * 1000
    head = (
        f"Same scale and seed, second run: {cmp['n']} format x workload x cache cells compared.\n\n"
        f"* Sizes: worst relative difference {100 * cmp['max_size_delta']:.4f} % "
        f"(tolerance {100 * SIZE_TOL:.0f} %): {ok}.\n"
        f"* Timings, all cells: {cmp['n'] - cmp['n_time_fail']} of {cmp['n']} within "
        f"{100 * TIME_TOL:.0f} %.\n"
        f"* Timings, cells with p50 >= {floor_ms:.0f} ms: "
        f"{cmp['n_big'] - cmp['n_big_time_fail']} of {cmp['n_big']} within "
        f"{100 * TIME_TOL:.0f} % (worst {100 * cmp['max_time_delta_big']:.1f} %).\n\n"
        "Worst timing deltas:\n\n"
    )
    body = [
        [c["format"], c["workload"], c["cache"], _sec(c["p50_a"]), _sec(c["p50_b"]),
         f"{100 * c['time_delta']:.1f} %"]
        for c in worst
    ]  # fmt: skip
    return head + _table(["Format", "Workload", "Cache", "Run A p50", "Run B p50", "Delta"], body)


# --- why F5 wins (measured) ----------------------------------------------------------------------
def encoding_ablation(table: pa.Table, workdir: Path) -> list[dict[str, Any]]:
    """Write ``table`` (sorted ticks) with F5's settings and with delta / byte-stream-split off.

    Everything except the toggled encodings comes from the contract and ``parquet_writer``
    (zstd level, data page size, row group size); the toggles are the only deviation. Returns
    compressed bytes for ``ts_ns``, ``price`` and the whole file.
    """
    base = pyarrow_write_kwargs(pw.schema_name_for("ticks"))
    enc: dict[str, str] = dict(base["column_encoding"])  # type: ignore[call-overload]
    floats = [c for c, e in enc.items() if "BYTE_STREAM_SPLIT" in e]
    variants = {
        "F5 as written": enc,
        "ts_ns without DELTA_BINARY_PACKED (PLAIN)": {c: e for c, e in enc.items() if c != "ts_ns"},
        "floats without BYTE_STREAM_SPLIT (PLAIN)": {
            c: e for c, e in enc.items() if c not in floats
        },
        "neither": {c: e for c, e in enc.items() if c != "ts_ns" and c not in floats},
    }
    out = []
    path = workdir / "ablation.parquet"
    for name, ce in variants.items():
        kw: dict[str, Any] = {**base, "column_encoding": ce, "use_dictionary": []}
        pq.write_table(
            table, path, compression=pw.COMPRESSION, compression_level=pw.COMPRESSION_LEVEL,
            data_page_size=pw.DATA_PAGE_BYTES, row_group_size=pw.ROW_GROUP_ROWS, **kw,
        )  # fmt: skip
        md = pq.ParquetFile(path).metadata
        per_col: dict[str, int] = defaultdict(int)
        for g in range(md.num_row_groups):
            for c in range(md.num_columns):
                col = md.row_group(g).column(c)
                per_col[col.path_in_schema] += col.total_compressed_size
        out.append({
            "variant": name, "rows": table.num_rows, "file_bytes": path.stat().st_size,
            "ts_ns_bytes": per_col["ts_ns"], "price_bytes": per_col["price"],
        })  # fmt: skip
        path.unlink()
    return out


def render_ablation(res: Sequence[dict[str, Any]]) -> str:
    """Bytes per tick for the timestamp and the price column under each encoding toggle."""
    n = res[0]["rows"]
    body = [
        [r["variant"], f"{r['ts_ns_bytes'] / n:.3f}", f"{r['price_bytes'] / n:.3f}",
         f"{r['file_bytes'] / n:.3f}"]
        for r in res
    ]  # fmt: skip
    return _table(
        ["Variant (zstd, same data)", "ts_ns B/tick", "price B/tick", "file B/tick"], body
    )


# --- flat table (the appendix) -------------------------------------------------------------------
def _cell(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6f} (MODELLED)"


def render_markdown(
    rows: list[ResultRow], prices: PriceBook, env: dict[str, str], *, internet_egress: bool = False
) -> str:
    """Results table (one line per format x workload x cache) plus env and EUR footnotes."""
    cols = [
        "Scale", "Format", "Workload", "Cache", "p50 s", "p95 s", "rows/s", "CPU s (client)",
        "Bytes at rest", "Bytes read", "EUR/month at rest", "EUR/1k queries (lower bound)",
    ]  # fmt: skip
    if internet_egress:
        cols.append("EUR/1k queries incl. internet egress")
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
    lines = []
    for r in sorted(rows, key=lambda x: (x.scale, format_sort_key(x.format_id), x.workload_id,
                                         x.cache, x.target)):  # fmt: skip
        rate = "n/a" if r.rows_per_s is None else f"{r.rows_per_s:,.0f}"
        read = "n/a" if r.bytes_read is None else f"{r.bytes_read:,}"
        rest = eur_per_month_at_rest(r.bytes_billed or r.bytes_at_rest, prices, r.storage_price_key)
        w1 = r.workload_id == "W1"
        q = None if w1 else eur_per_1k_queries(r.bytes_read, prices)
        name = r.format_id + (" [azurite]" if r.target == "azurite" else "")
        cells = [
            r.scale, name, r.workload_id, r.cache, f"{r.wall_p50_s:.4f}", f"{r.wall_p95_s:.4f}",
            rate, f"{r.cpu_s:.3f}", f"{r.bytes_at_rest:,}", read, _cell(rest), _cell(q),
        ]  # fmt: skip
        if internet_egress:
            q2 = None if w1 else eur_per_1k_queries(r.bytes_read, prices, internet_egress=True)
            cells.append(_cell(q2))
        lines.append("| " + " | ".join(cells) + " |")
    envs = ", ".join(f"{k}={v}" for k, v in sorted(env.items()))
    foot = (
        f"\nWall, CPU and byte columns are MEASURED; prices captured {prices.captured_at} "
        f"({prices.region}, {prices.currency}).\n\n"
        'Caveats: "cold" means a fresh connection only; the OS page cache is NOT dropped, so '
        "cold understates a truly cold read. CPU s is client-process CPU only (server-side SQL "
        "CPU is not included). W1 has no cache state (fresh store per run).\n\n"
        "[MODELLED] EUR/month at rest = billed bytes / 2^30 x unitPrice(the format's "
        "`storage_price_key`; allocated pages for SQL, stored bytes for Blob).\n\n"
        f"[MODELLED] EUR/1k queries = 1000 x ceil(bytes_read / 4 MiB) / 10,000 x `{READ_KEY}`. "
        "The 4 MiB-per-GET operation count is a lower bound (Azurite runs count real requests). "
        "Egress is 0: queries run from Container Apps in the same region (SPEC-02 section 5)."
    )
    if internet_egress:
        foot += (
            f" The internet-egress column adds bytes_read / 2^30 x `{EGRESS_KEY}` (first paid "
            "tier, an upper bound)."
        )
    foot += (
        "\n\nCompute is excluded: Container Apps per-second meters are precision-limited, so "
        "compute is reported MEASURED from Cost Management (C5), never MODELLED here.\n\n"
        f"Environment: {envs}\n"
    )
    return f"{head}\n" + "\n".join(lines) + "\n" + foot


# --- charts --------------------------------------------------------------------------------------
def _save(fig: Any, path: Path) -> Path:  # Any: matplotlib Figure (untyped)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return path


def render_charts(rows: list[ResultRow], img_dir: Path) -> list[Path]:
    """Charts of every scale present in ``rows`` (no price panel)."""
    out: list[Path] = []
    for scale in _scales(rows):
        out += render_scale_charts(rows, None, img_dir, scale)
    return out


def _chart_size(rows: Sequence[ResultRow], prices: PriceBook | None, img: Path, scale: str) -> Path:
    lines = size_lines(rows, scale)
    labels = [_short(ln.label) for ln in lines]
    fig, axes = plt.subplots(1, 2 if prices else 1, figsize=(11 if prices else 6, 4), squeeze=False)
    ax = axes[0][0]
    ax.bar(labels, [ln.used / GIB for ln in lines])
    ax.set_ylabel("GiB at rest (MEASURED, used)")
    ax.set_title(f"Storage size by format, scale {scale}")
    ax.tick_params(axis="x", rotation=60)
    if prices:
        ax2 = axes[0][1]
        eur = [eur_per_month_at_rest(ln.billed, prices, ln.price_key) for ln in lines]
        ax2.bar(labels, eur, color="tab:green")
        ax2.set_ylabel("EUR / month at rest (MODELLED)")
        ax2.set_title("Billing basis: allocated (SQL) / stored (Blob)")
        ax2.tick_params(axis="x", rotation=60)
    return _save(fig, img / f"size_eur_{scale}.png")


def _chart_p50(rows: Sequence[ResultRow], img: Path, scale: str) -> Path:
    fids = _fids(rows, scale, sweep=False)
    fig, ax = plt.subplots(figsize=(9, 4))
    width = 0.8 / max(len(fids), 1)
    for i, f in enumerate(fids):
        vals = []
        for w in QUERY_WLS:
            r = _main_row(rows, scale, f, w, "warm")
            vals.append(r.wall_p50_s if r else 0.0)
        ax.bar([k + i * width for k in range(len(QUERY_WLS))], vals, width, label=f)
    ax.set_xticks([k + 0.4 - width / 2 for k in range(len(QUERY_WLS))], list(QUERY_WLS))
    ax.set_yscale("log")
    ax.set_ylabel("warm p50 wall s (MEASURED)")
    ax.set_title(f"Query latency, scale {scale}")
    ax.legend(ncol=4, fontsize=7)
    return _save(fig, img / f"p50_{scale}.png")


def _chart_w1(rows: Sequence[ResultRow], img: Path, scale: str) -> Path | None:
    pairs = [(f, _main_row(rows, scale, f, "W1", "n/a")) for f in _all_fids(rows, scale)]
    pts = [(f, r.rows_per_s) for f, r in pairs if r is not None and r.rows_per_s]
    if not pts:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.bar([f for f, _ in pts], [v or 0 for _, v in pts], color="tab:orange")
    ax.set_yscale("log")
    ax.set_ylabel("W1 rows/s (MEASURED)")
    ax.tick_params(axis="x", rotation=60)
    return _save(fig, img / f"w1_{scale}.png")


def _chart_bytes_read(rows: Sequence[ResultRow], img: Path, scale: str) -> Path | None:
    classes: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for f in _fids(rows, scale, sweep=False):
        for w in QUERY_WLS:
            r = _main_row(rows, scale, f, w, "warm")
            if r and r.bytes_read is not None:
                classes[bytes_read_class(r.bytes_read_source)][f][w] = r.bytes_read / MIB
    if not classes:
        return None
    fig, axes = plt.subplots(1, len(classes), figsize=(5.5 * len(classes), 4), squeeze=False)
    for ax, (cls, per_fmt) in zip(axes[0], sorted(classes.items()), strict=True):
        fs = sorted(per_fmt, key=format_sort_key)
        wd = 0.8 / len(fs)
        for i, f in enumerate(fs):
            xs = [k + i * wd for k in range(len(QUERY_WLS))]
            ax.bar(xs, [per_fmt[f].get(w, 0) for w in QUERY_WLS], wd, label=f)
        ax.set_xticks([k + 0.4 - wd / 2 for k in range(len(QUERY_WLS))], list(QUERY_WLS))
        ax.set_yscale("log")
        ax.set_ylabel("MiB read (MEASURED)")
        ax.set_title(f"source class: {cls}")
        ax.legend(fontsize=7)
    return _save(fig, img / f"bytes_read_{scale}.png")


def _chart_sweep(rows: Sequence[ResultRow], img: Path, scale: str) -> Path | None:
    grid: dict[tuple[int, str], float] = {}
    for f in _fids(rows, scale, sweep=True):
        m, r = re.fullmatch(r"F6-z(\d+)-rg(\w+)", f), _size_row(rows, scale, f)
        if m and r:
            grid[(int(m[1]), m[2])] = r.bytes_at_rest / MIB
    if not grid:
        return None
    levels = sorted({lv for lv, _ in grid})
    rgs = sorted(
        {rg for _, rg in grid}, key=lambda s: int(s[:-1]) * (1000 if s[-1] == "k" else 10**6)
    )
    fig, ax = plt.subplots(figsize=(5, 3.5))
    im = ax.imshow([[grid.get((lv, rg), 0.0) for rg in rgs] for lv in levels], cmap="viridis_r")
    ax.set_xticks(range(len(rgs)), rgs)
    ax.set_yticks(range(len(levels)), [f"zstd {lv}" for lv in levels])
    for i, lv in enumerate(levels):
        for j, rg in enumerate(rgs):
            ax.text(j, i, f"{grid.get((lv, rg), 0):,.0f}", ha="center", va="center", color="w")
    fig.colorbar(im, label="MiB at rest (MEASURED)")
    ax.set_title(f"F6 sweep, scale {scale}")
    return _save(fig, img / f"f6_sweep_{scale}.png")


def _chart_requests(rows: Sequence[ResultRow], img: Path, scale: str) -> Path | None:
    pts = []
    for f in _fids(rows, scale, sweep=False):
        for w in QUERY_WLS:
            r = _find(rows, scale, f, w, "warm", target="azurite")
            if r is not None and r.requests_get is not None:
                pts.append((f"{f}\n{w}", r.requests_get, r.requests_list or 0))
    if not pts:
        return None
    fig, ax = plt.subplots(figsize=(10, 3.5))
    labels = [p[0] for p in pts]
    ax.bar(labels, [p[1] for p in pts], label="GET")
    ax.bar(labels, [p[2] for p in pts], bottom=[p[1] for p in pts], label="LIST")
    ax.set_yscale("log")
    ax.set_ylabel("HTTP requests per query (MEASURED)")
    ax.tick_params(axis="x", labelsize=6)
    ax.legend()
    return _save(fig, img / f"requests_{scale}.png")


def render_scale_charts(
    rows: Sequence[ResultRow], prices: PriceBook | None, img_dir: Path, scale: str
) -> list[Path]:
    """All charts of one scale; a chart whose inputs were not run is skipped."""
    img_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path | None] = []
    if size_lines(rows, scale):
        out.append(_chart_size(rows, prices, img_dir, scale))
        out.append(_chart_p50(rows, img_dir, scale))
    out += [
        _chart_w1(rows, img_dir, scale),
        _chart_bytes_read(rows, img_dir, scale),
        _chart_sweep(rows, img_dir, scale),
        _chart_requests(rows, img_dir, scale),
    ]
    return [p for p in out if p is not None]


# --- the report ----------------------------------------------------------------------------------
def exec_summary(rs: RunSet, prices: PriceBook, scale: str) -> str:
    """Three business bullets whose numbers are all taken from the measured rows."""
    h = headline_ratios(rs.rows, prices, scale)

    def warm(fid: str, wl: str) -> float | None:
        r = _main_row(rs.rows, scale, fid, wl, "warm")
        return r.wall_p50_s if r else None

    f5 = next((ln for ln in size_lines(rs.rows, scale) if ln.label == "F5"), None)
    n = rs.ds_rows.get(scale)
    ticks = f" ({n / 1e6:,.1f} million ticks)" if n else ""
    held = f" in {_bytes(f5.used)}" if f5 else ""
    b1 = (
        f"* **Smaller and cheaper to keep.** Tidal's epoch-optimised Parquet (F5) holds the "
        f"scale-{scale} dataset{ticks}{held}: {_x(h['size_f0_raw_over_f5'])} smaller than the "
        f"naive relational baseline F0 ({_x(h['size_f0_adj_over_f5'])} after removing the extra "
        f"column F0 needs for nanosecond accuracy) and {_x(h['size_f4_over_f5'])} smaller than "
        f"default Parquet. At list prices that is {_x(h['eur_f0_raw_over_f5'])} lower monthly "
        f"storage cost than F0 ({_x(h['eur_f0_adj_over_f5'])} adjusted), because the files also "
        "sit in the cheaper Blob tier (MEASURED bytes, MODELLED EUR)."
    )
    sp = [(wl, _ratio(warm("F0", wl), warm("F5", wl))) for wl in ("W2", "W3", "W5", "W6")]
    speeds = ", ".join(f"{_x(v)} ({wl})" for wl, v in sp)
    w4 = _x(_ratio(warm("F0", "W4"), warm("F5", "W4")))
    b2 = (
        f"* **Faster analytical reads.** On the same machine, warm queries over F5 take a "
        f"fraction of F0's time: F0/F5 = {speeds} (W2 daily bars, W3 one-month range, W5 "
        f"ten-instrument scan, W6 as-of with restatements). The point lookup (W4) is {w4} (F0/F5), "
        "where a clustered index is at its best. Local, in-process numbers: see the caveats."
    )
    az = _find(rs.rows, scale, "F5", "W3", "warm", target="azurite")
    cost = eur_per_1k_from_requests(az, prices) if az else None
    w3 = _main_row(rs.rows, scale, "F5", "W3", "warm")
    frac = _ratio(w3.bytes_read, w3.bytes_at_rest) if w3 else None
    share = "n/a" if frac is None else f"{100 * frac:.2f} %"
    tail = "."
    if az is not None and cost is not None:
        tail = (
            f" and, counted against Azurite, issues {az.requests_get} GET + {az.requests_list} "
            f"LIST requests, about EUR {cost:.4f} of Blob transactions per 1,000 queries "
            "(MODELLED from MEASURED counts)."
        )
    b3 = (
        "* **Reads are cheap per query because most of the file is skipped.** A one-month "
        f"range query on F5 reads {share} of the stored bytes (row-group statistics let the "
        f"engine skip the rest){tail} Absolute Azure numbers are pending (local run only)."
    )
    return "\n".join([b1, b2, b3]) + "\n"


_METHOD: Final = """## Methodology

* Data: deterministic synthetic ticks (`tidal.ingest.sources.synthetic`, seed 42), one object per
  instrument-month, plus 1 % restatements (`version=2`) of one instrument-month for W6.
  {datasets}.
* F0-F2 run on SQL Server 2022 (Docker, Developer edition); F3-F6 are files read by DuckDB, on the
  local file system for timings and additionally on Azurite (through a counting proxy) for real
  HTTP request counts and egress bytes. Candidates: SPEC-02 section 2, with the T-202/T-203
  deviations (F0 `ts_sub` column, F4 `timestamp[ns]`).
* Workloads W1-W6: SPEC-02 section 3. Every result of every format is checked against an
  independent polars oracle on every run; a mismatch aborts the run.
* 5 runs per cell (SQL W1: fewer, see the W1 table), p50 and p95 reported. cold = fresh connection
  (SQL also empties its buffer pool); warm = after one untimed priming run. W1 = fresh store/run.
* Bytes read: DuckDB profile (local Parquet), whole-file size (CSV), real HTTP response bytes
  (Azurite), SQL `STATISTICS IO` logical pages x 8 KiB. These measure different things; tables
  label the source class and the chart gives each class its own axis.
* At rest: Azure SQL bills the provisioned maximum data size and never less than what is
  allocated, so the SQL EUR column uses the table's allocated (reserved) pages, not used pages;
  Blob bills stored bytes. Allocated is the tightest basis one table measurement supports (real
  bills add system objects, the log and headroom), so SQL cost is if anything understated.
  F0 is shown raw and adjusted (minus the 1 byte/tick `ts_sub` that only exists for nanosecond
  exactness).
* EUR per 1,000 queries: Blob transactions from REAL Azurite request counts (GET/HEAD at the read
  meter, LIST at the write/list meter, in-region egress free). SQL has no per-operation meter, so
  its column is modelled compute (1 vCore busy for the wall time at the serverless rate).
  File-format compute (Container Apps) is excluded (C5); the families are therefore not directly
  comparable on this column, compare the time columns.
"""

_CAVEATS: Final = """## Caveats

* Local only: one Windows laptop, SQL Server in Docker, Azurite. No Azure number here; Azure runs
  are NEEDS-CLOUD and absolute latencies will differ (network, Container Apps 1 vCPU / 2 GiB).
* Cold/warm asymmetry: SQL empties its buffer pool (`DBCC DROPCLEANBUFFERS`), files only get a
  fresh DuckDB connection, so the OS page cache stays warm and file "cold" understates a truly
  cold read.
* TDS (SQL, a socket and ODBC) vs in-process DuckDB: part of the latency gap is protocol and
  process boundary, not storage format. The SQL W1 load is Python-bound (`fast_executemany`
  batches), so SQL write throughput says more about the loader than about the engine.
* SQL W2 plan shape: W2 is a grouped pass plus two index seeks per day for open/close; another
  formulation could change the F0-F2 numbers.
* SQL CPU is not in the CPU column (client process only); SQL bytes read are logical pages, not
  physical I/O.
* F6 row groups: an instrument-month holds fewer ticks than one 1M-row group at the larger rows
  per month, so the 1m and 4m settings can produce identical files; see the row-group table.
* Synthetic data: compression ratios depend on the generator's price process and tick spacing.
"""


def render_report(
    rs: RunSet,
    prices: PriceBook,
    *,
    date_str: str,
    repro: dict[str, Any] | None = None,
    ablation: Sequence[dict[str, Any]] | None = None,
    ablation_note: str = "",
    img_rel: str = "img",
) -> str:
    """The full Markdown report."""
    scales = _scales(rs.rows)
    main = scales[0] if scales else "S"
    ds = ", ".join(f"scale {s}: {rs.ds_rows[s]:,} base ticks" for s in scales if s in rs.ds_rows)
    parts = [
        f"# Storage format benchmark, {date_str}\n",
        "_Generated by `benchmarks/storage/report.py` from the raw results Parquet. Every number "
        "is MEASURED (harness) or MODELLED (EUR = quantity x `docs/finops/prices.yaml`) and "
        "labelled so. Local run only; Azure runs are NEEDS-CLOUD._\n",
        "## Executive summary\n",
        exec_summary(rs, prices, main),
        _METHOD.format(datasets=ds or "dataset sizes not recorded"),
    ]
    for scale in scales:
        n = rs.ds_rows.get(scale)
        rows = rs.rows
        parts += [
            f"## Results, scale {scale}\n",
            "### Storage at rest\n\n"
            f"![size and cost]({img_rel}/size_eur_{scale}.png)\n\n"
            + render_size_table(rows, prices, scale, n),
            "### W1 bulk write (one instrument)\n\n"
            + render_w1_table(rows, scale)
            + f"\n![W1]({img_rel}/w1_{scale}.png)\n",
            "### Query latency, p50 seconds\n\nWarm:\n\n" + render_query_table(rows, scale, "warm"),
            "Cold (fresh connection; see the cold_mode table):\n\n"
            + render_query_table(rows, scale, "cold"),
            f"![p50]({img_rel}/p50_{scale}.png)\n",
            "cold_mode per format (`dropcleanbuffers`: SQL buffer pool emptied; "
            "`fresh_connection`: files, OS page cache NOT dropped):\n\n"
            + render_cold_modes(rows, scale),
            "### Bytes read\n\n"
            + render_io_table(rows, scale)
            + f"\n![bytes read]({img_rel}/bytes_read_{scale}.png)\n",
        ]
        rq = render_requests_table(rows, prices, scale)
        if rq.count("\n") > 2:
            parts.append(
                "### Blob requests (Azurite, REAL counts per query)\n\n"
                + rq
                + f"\n![requests]({img_rel}/requests_{scale}.png)\n"
            )
        parts.append(
            "### EUR per 1,000 queries (MODELLED)\n\n" + render_eur_query_table(rows, prices, scale)
        )
        parts.append("### Row groups\n\n" + render_rowgroup_table(rows, scale))
        if _fids(rows, scale, sweep=True):
            parts.append(
                "### F6 sweep (zstd level x row-group size)\n\n"
                + render_sweep_table(rows, scale)
                + f"\n![F6 sweep]({img_rel}/f6_sweep_{scale}.png)\n"
            )
    parts.append(
        "## Why F5 wins\n\nShare of the stored bytes a query actually reads (warm, "
        "scale " + main + "):\n\n" + skip_table(rs.rows, main)
    )
    if ablation:
        parts.append(
            f"Encoding ablation, one instrument-month{ablation_note}; MEASURED compressed "
            "column bytes per tick:\n\n" + render_ablation(ablation)
        )
    parts.append(
        "* Delta encoding: tick timestamps are sorted and close together, so DELTA_BINARY_PACKED "
        "stores small deltas; the ablation table gives the measured ts_ns bytes per tick.\n"
        "* Byte-stream-split: it regroups the float bytes so zstd sees the near-constant exponent "
        "bytes together; see the price column.\n"
        "* Statistics skipping: files are sorted on `ts_ns` with min/max statistics, so W3/W4 "
        "touch only the row groups of the requested range; the table above is the measured "
        "effect.\n"
    )
    if repro:
        parts.append("## Reproducibility\n\n" + render_repro(repro))
    not_run = "\n".join(f"* {x}" for x in rs.not_run) or "Nothing recorded."
    parts += ["## Not run\n\n" + not_run + "\n", _CAVEATS]
    parts.append("## Appendix, every cell\n\n" + render_markdown(rs.rows, prices, rs.env))
    return "\n".join(parts)


def _explain_table(dataset_root: Path | None) -> tuple[pa.Table, str] | None:
    if dataset_root is None:
        return None
    files = sorted((dataset_root / "base").rglob("*.parquet"))
    if not files:
        return None
    pick = files[len(files) // 2]
    return pq.read_table(pick), f" (`{'/'.join(pick.parts[-4:])}`)"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(prog="benchmarks.storage.report")
    ap.add_argument("--results", type=Path, nargs="+", required=True, help="dirs with results")
    ap.add_argument("--repro", type=Path, nargs=2, metavar=("A", "B"), help="two runs to compare")
    ap.add_argument("--prices", type=Path, default=Path("docs/finops/prices.yaml"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--img", type=Path, default=Path("docs/finops/results/img"))
    ap.add_argument("--date", default="2026-10-22")
    ap.add_argument("--explain-dataset", type=Path, default=None, help=".data/bench/<key> dir")
    ap.add_argument("--not-run", action="append", default=[], help="a line for 'Not run'")
    a = ap.parse_args(argv)
    rs = load_runs(a.results)
    rs.not_run = list(a.not_run)
    prices = load_prices(a.prices)
    repro = None
    if a.repro:
        ra, _ = read_results(a.repro[0] / "results.parquet")
        rb, _ = read_results(a.repro[1] / "results.parquet")
        repro = compare_runs(ra, rb)
    abl, note = None, ""
    ex = _explain_table(a.explain_dataset)
    if ex is not None:
        with tempfile.TemporaryDirectory() as d:
            abl, note = encoding_ablation(ex[0], Path(d)), ex[1]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    md = render_report(rs, prices, date_str=a.date, repro=repro, ablation=abl, ablation_note=note)
    a.out.write_text(md, encoding="utf-8")
    for scale in _scales(rs.rows):
        render_scale_charts(rs.rows, prices, a.img, scale)


if __name__ == "__main__":
    main()
