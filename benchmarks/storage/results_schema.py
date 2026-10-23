"""One benchmark measurement: pydantic row model, pyarrow schema and Parquet round trip."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION: Final = "2"

#: bytes_read_source -> comparability class. Values of different classes must not share an axis.
BYTES_READ_CLASS: Final = {
    "duckdb_profile": "storage_bytes",  # compressed bytes DuckDB read from local Parquet
    "file_size": "storage_bytes",  # whole-file size of the CSV objects touched
    "azurite_http_response_bytes": "http_bytes",  # real bytes on the wire (Blob mode)
    "sql_statistics_io": "logical_pages",  # 8 KiB pages touched, buffer-pool independent
    "n/a": "n/a",
}


class ResultRow(BaseModel):
    """Wall/CPU/bytes for one (format, workload, cache state). All fields are MEASURED.

    EUR columns are never stored: they are MODELLED from ``prices.yaml`` at report time.
    ``bytes_read`` is None when the format cannot report it. ``rows`` is None when the engine
    does not report rows scanned (SQL W2-W6: shown as n/a, never 0). Blob-only columns
    (``requests_*``, ``egress_bytes``) are None for local-file and SQL rows.
    """

    model_config = ConfigDict(frozen=True)

    dataset_key: str
    scale: str
    target: str = "local"  # "local" (file system) | "azurite" | "sql_server"
    seed: int
    format_id: str
    workload_id: str
    cache: Literal["cold", "warm", "n/a"]  # W1 = fresh-store write: n/a
    runs: int
    wall_p50_s: float
    wall_p95_s: float
    cpu_s: float  # median CLIENT-process CPU s (all threads); server-side SQL CPU is NOT included
    rows: int | None  # rows written (W1) or rows scanned by the engine (W2-W6); None = n/a
    rows_per_s: float | None  # rows / wall_p50_s
    bytes_at_rest: int
    bytes_read: int | None
    bytes_read_source: str  # a key of BYTES_READ_CLASS
    storage_price_key: str  # prices.yaml key used for the at-rest EUR column
    # --- schema v2 -------------------------------------------------------------------------
    bytes_billed: int | None = None  # basis the provider bills at rest (SQL: allocated pages)
    bytes_at_rest_adjusted: int | None = None  # F0 minus the ts_sub byte/row; None = no adjust
    cold_mode: str = "n/a"  # cold rows: dropcleanbuffers | reconnect_only | fresh_connection
    row_groups_files: int | None = None  # Parquet files (None for CSV / SQL)
    row_groups_total: int | None = None
    row_groups_max: int | None = None
    requests_get: int | None = None  # real HTTP counts (Blob mode, io-profile pass)
    requests_head: int | None = None
    requests_list: int | None = None
    requests_other: int | None = None
    egress_bytes: int | None = None


_FIELDS: Final["list[pa.Field[Any]]"] = [  # Any: pyarrow-stubs Field is generic in the type
    pa.field("dataset_key", pa.string(), nullable=False),
    pa.field("scale", pa.string(), nullable=False),
    pa.field("target", pa.string(), nullable=False),
    pa.field("seed", pa.int64(), nullable=False),
    pa.field("format_id", pa.string(), nullable=False),
    pa.field("workload_id", pa.string(), nullable=False),
    pa.field("cache", pa.string(), nullable=False),
    pa.field("runs", pa.int32(), nullable=False),
    pa.field("wall_p50_s", pa.float64(), nullable=False),
    pa.field("wall_p95_s", pa.float64(), nullable=False),
    pa.field("cpu_s", pa.float64(), nullable=False),
    pa.field("rows", pa.int64(), nullable=True),
    pa.field("rows_per_s", pa.float64(), nullable=True),
    pa.field("bytes_at_rest", pa.int64(), nullable=False),
    pa.field("bytes_read", pa.int64(), nullable=True),
    pa.field("bytes_read_source", pa.string(), nullable=False),
    pa.field("storage_price_key", pa.string(), nullable=False),
    pa.field("bytes_billed", pa.int64(), nullable=True),
    pa.field("bytes_at_rest_adjusted", pa.int64(), nullable=True),
    pa.field("cold_mode", pa.string(), nullable=False),
    pa.field("row_groups_files", pa.int64(), nullable=True),
    pa.field("row_groups_total", pa.int64(), nullable=True),
    pa.field("row_groups_max", pa.int64(), nullable=True),
    pa.field("requests_get", pa.int64(), nullable=True),
    pa.field("requests_head", pa.int64(), nullable=True),
    pa.field("requests_list", pa.int64(), nullable=True),
    pa.field("requests_other", pa.int64(), nullable=True),
    pa.field("egress_bytes", pa.int64(), nullable=True),
]
RESULT_SCHEMA: Final = pa.schema(_FIELDS)


def bytes_read_class(source: str) -> str:
    """Comparability class of a ``bytes_read_source`` (unknown sources are their own class)."""
    return BYTES_READ_CLASS.get(source, source)


def to_table(rows: Sequence[ResultRow], env: dict[str, str] | None = None) -> pa.Table:
    """Rows -> Arrow table; the machine/library versions ride along as schema metadata."""
    meta = {"tidal.bench.schema": SCHEMA_VERSION, **{f"env.{k}": v for k, v in (env or {}).items()}}
    cols = [r.model_dump() for r in rows]
    return pa.Table.from_pylist(cols, schema=RESULT_SCHEMA.with_metadata(meta))


def from_table(table: pa.Table) -> list[ResultRow]:
    """Inverse of :func:`to_table` (validates every row)."""
    return [ResultRow.model_validate(d) for d in table.to_pylist()]


def write_results(path: Path, rows: Sequence[ResultRow], env: dict[str, str] | None = None) -> None:
    """Write the raw results as Parquet."""
    pq.write_table(to_table(rows, env), path, compression="zstd")


def read_results(path: Path) -> tuple[list[ResultRow], dict[str, str]]:
    """Read rows and the recorded environment back from a results Parquet file."""
    table = pq.read_table(path)
    meta = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    env = {k.removeprefix("env."): v for k, v in meta.items() if k.startswith("env.")}
    return from_table(table), env
