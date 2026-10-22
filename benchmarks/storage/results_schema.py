"""One benchmark measurement: pydantic row model, pyarrow schema and Parquet round trip."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION: Final = "1"


class ResultRow(BaseModel):
    """Wall/CPU/bytes for one (format, workload, cache state). All fields are MEASURED.

    EUR columns are never stored: they are MODELLED from ``prices.yaml`` at report time.
    ``bytes_read`` is None when the format cannot report it (hook: SQL ``STATISTICS IO``).
    """

    model_config = ConfigDict(frozen=True)

    dataset_key: str
    scale: str
    seed: int
    format_id: str
    workload_id: str
    cache: Literal["cold", "warm"]
    runs: int
    wall_p50_s: float
    wall_p95_s: float
    cpu_s: float  # median process CPU seconds (all threads) per run
    rows: int  # rows written (W1) or rows scanned by the engine (W2-W6)
    rows_per_s: float | None  # rows / wall_p50_s
    bytes_at_rest: int
    bytes_read: int | None
    bytes_read_source: str  # "duckdb_profile" | "sql_statistics_io" | "n/a"
    storage_price_key: str  # prices.yaml key used for the at-rest EUR column


_FIELDS: Final["list[pa.Field[Any]]"] = [  # Any: pyarrow-stubs Field is generic in the type
    pa.field("dataset_key", pa.string(), nullable=False),
    pa.field("scale", pa.string(), nullable=False),
    pa.field("seed", pa.int64(), nullable=False),
    pa.field("format_id", pa.string(), nullable=False),
    pa.field("workload_id", pa.string(), nullable=False),
    pa.field("cache", pa.string(), nullable=False),
    pa.field("runs", pa.int32(), nullable=False),
    pa.field("wall_p50_s", pa.float64(), nullable=False),
    pa.field("wall_p95_s", pa.float64(), nullable=False),
    pa.field("cpu_s", pa.float64(), nullable=False),
    pa.field("rows", pa.int64(), nullable=False),
    pa.field("rows_per_s", pa.float64(), nullable=True),
    pa.field("bytes_at_rest", pa.int64(), nullable=False),
    pa.field("bytes_read", pa.int64(), nullable=True),
    pa.field("bytes_read_source", pa.string(), nullable=False),
    pa.field("storage_price_key", pa.string(), nullable=False),
]
RESULT_SCHEMA: Final = pa.schema(_FIELDS)


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
