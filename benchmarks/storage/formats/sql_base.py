"""Shared machinery for the SQL Server candidates F0-F2 (SPEC-02 section 2).

All three store the same tick table as ``md.tick_hot`` (one table per format, one throwaway schema
per format instance) and answer W2-W6 with T-SQL; only the physical design differs:

* F0 ``DATETIME2(7)`` time column, no compression (the naive baseline),
* F1 ``ts_ns BIGINT``, PAGE compression, clustered PK (the tuned rowstore),
* F2 ``ts_ns BIGINT`` in an ordered clustered columnstore index.

Connection: pyodbc directly (``fast_executemany`` for loads, ``STATISTICS IO`` messages for the
bytes-read metric). pyodbc is imported lazily, so a files-only runner does not need libodbc.
Every SQL touchpoint takes a :class:`SqlTarget`, so tests point it at a throwaway database.
"""

# ruff: noqa: S608 - SQL is assembled from class constants and generated identifiers only;
# every value (instrument keys, bounds, as-of instants) is a bound parameter.

import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal

import numpy as np
import polars as pl
import pyarrow as pa
import structlog

from benchmarks.storage.workloads import (
    Dataset,
    LoadStats,
    Value,
    WorkloadResult,
    _bounds,
)
from tidal.common.errors import ConfigError

if TYPE_CHECKING:
    import pyodbc

log = structlog.get_logger(__name__)

PAGE_BYTES: Final = 8192
BATCH_ROWS: Final = 102_400  # rows per fast_executemany call and commit (columnstore rowgroup min)
_INSERT_COLS: Final = "instrument_key, {time}, price, size, side, quality, ingested_at_ns, version"
_TABLE_LINE = re.compile(r"Table '(?P<name>[^']+)'\. Scan count")  # after the ODBC prefix
_LOGICAL = re.compile(r"(?<!lob )logical reads (\d+)")
_LOB_LOGICAL = re.compile(r"lob logical reads (\d+)")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "(local)", "."})
ColdMode = Literal["dropcleanbuffers", "reconnect_only"]


@dataclass(frozen=True)
class SqlTarget:
    """Where a SQL format runs: ``server`` is the ODBC form (``host,port``)."""

    server: str
    database: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "SqlTarget":
        """Build from ``TIDAL_TEST_SQL_URL`` (else ``TIDAL_SQL__SERVER``) and ``TIDAL_SQL__*``.

        The password comes from ``TIDAL_TEST_SQL_PASSWORD`` or ``TIDAL_SQL__PASSWORD`` and is
        never logged or included in error messages.
        """
        env = os.environ
        server = env.get("TIDAL_TEST_SQL_URL") or env.get("TIDAL_SQL__SERVER")
        password = env.get("TIDAL_TEST_SQL_PASSWORD") or env.get("TIDAL_SQL__PASSWORD")
        database = env.get("TIDAL_SQL__DATABASE")
        if not (server and password and database):
            raise ConfigError(
                "SQL formats need TIDAL_TEST_SQL_URL (or TIDAL_SQL__SERVER), TIDAL_SQL__DATABASE"
                " and TIDAL_TEST_SQL_PASSWORD (or TIDAL_SQL__PASSWORD)"
            )
        return cls(server, database, env.get("TIDAL_SQL__USER", "sa"), password)

    @property
    def is_local(self) -> bool:
        """Whether the server is on this machine (loopback host)."""
        return self.server.split(",")[0].strip().lower() in _LOOPBACK

    def odbc_string(self, driver: str, *, database: str | None = None) -> str:
        """ODBC connection string; ``}`` in the password is escaped, the certificate is only
        trusted blindly for a local container (managed servers present a valid chain)."""
        pwd = self.password.replace("}", "}}")
        trust = "yes" if self.is_local else "no"
        return (
            f"DRIVER={{{driver}}};SERVER={self.server};DATABASE={database or self.database};"
            f"UID={self.user};PWD={{{pwd}}};TrustServerCertificate={trust}"
        )

    def connect(self, *, database: str | None = None) -> "pyodbc.Connection":
        """Open an autocommit connection (newest installed SQL Server ODBC driver)."""
        import pyodbc  # noqa: PLC0415 - lazy: file-only runs must not need libodbc

        drivers = sorted(
            (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
            key=lambda d: int(d.split()[2]),
        )
        if not drivers:
            raise ConfigError("no 'ODBC Driver N for SQL Server' installed")
        return pyodbc.connect(
            self.odbc_string(drivers[-1], database=database), autocommit=True, timeout=30
        )


def dt2_text(ts_ns: "np.typing.NDArray[np.int64]") -> list[str]:
    """Epoch ns -> ``YYYY-MM-DD HH:MM:SS.fffffff`` (floored to the 100 ns DATETIME2(7) tick)."""
    s = pl.Series(ts_ns).cast(pl.Datetime("ns", "UTC"))
    return s.dt.to_string("%Y-%m-%d %H:%M:%S%.9f").str.slice(0, 27).to_list()


def month_range(months: Sequence[Sequence[int]]) -> tuple[int, int]:
    """``[lo_ns, hi_ns)`` covering the first through the last (year, month)."""
    ms = sorted((int(y), int(m)) for y, m in months)
    return _bounds(*ms[0])[0], _bounds(*ms[-1])[1]


def parse_io_pages(messages: Sequence[tuple[str, str]]) -> int:
    """Pages read from ``SET STATISTICS IO`` messages: logical + lob logical reads of the
    ``tick_fX`` table only (Worktable / Workfile spills are engine temp I/O, not table reads)."""
    pages = 0
    for _state, text in messages:
        for line in text.splitlines():
            m = _TABLE_LINE.search(line)
            if m is None or not m["name"].startswith("tick_"):
                continue
            pages += sum(int(x) for x in _LOGICAL.findall(line))
            pages += sum(int(x) for x in _LOB_LOGICAL.findall(line))
    return pages


class SqlFormat:
    """Base for F0-F2; subclasses set the time representation and the physical design."""

    id: str
    storage_price_key: str = "sql_gp_storage_gb_month"
    time_cols: ClassVar[tuple[str, ...]]  # columns that hold the timestamp, in key order
    ns_expr: ClassVar[str]  # T-SQL expression: exact epoch ns of a row
    day_expr: ClassVar[str]  # T-SQL expression: UTC day bucket of a row
    ddl: ClassVar[tuple[str, ...]]  # statements with {t} (qualified name) and {pk}
    post_load: ClassVar[tuple[str, ...]] = ()  # statements run after the last insert (with {t})

    def __init__(self, _workdir: Path, target: SqlTarget | None = None) -> None:
        """The work directory is unused (state lives in SQL Server); ``target`` defaults to env."""
        self.target = target or SqlTarget.from_env()
        self.schema = f"bench_{uuid.uuid4().hex[:10]}"
        self.table = f"[{self.schema}].[tick_{self.id.lower()}]"
        self._con: pyodbc.Connection | None = None
        self._keys: dict[str, int] = {}
        self._created = False
        self.cold_mode: ColdMode = "dropcleanbuffers"
        self.rows_loaded = 0

    # --- connection -------------------------------------------------------------------------
    def _conn(self) -> "pyodbc.Connection":
        if self._con is None:
            self._con = self.target.connect()
        return self._con

    def _create(self) -> None:
        con = self._conn()
        con.execute(f"CREATE SCHEMA [{self.schema}]")
        self._created = True  # from here teardown must drop whatever exists
        for stmt in self.ddl:
            con.execute(stmt.format(t=self.table, pk=f"pk_{self.schema}"))

    # --- Format protocol --------------------------------------------------------------------
    def load(self, dataset: Dataset) -> LoadStats:
        """Insert base ticks (time order), then the restatements; then ``post_load``."""
        if not self._created:
            self._create()
        self._keys = {inst: k + 1 for k, inst in enumerate(sorted(dataset.instruments))}
        total = 0
        for part in dataset.partitions():
            total += self._insert(dataset.read_partition(part), self._keys[part[0]])
        if dataset.restated[0] in dataset.instruments:
            tables = dataset.read_partition(dataset.restated, restated=True)
            total += self._insert(tables, self._keys[dataset.restated[0]])
        con = self._conn()
        for stmt in self.post_load:
            con.execute(stmt.format(t=self.table))
        self.rows_loaded += total
        return LoadStats(total, self.size_bytes())

    def size_correction_bytes(self) -> int:
        """Bytes in :meth:`size_bytes` that exist only to keep ``ts_ns`` exact (F0: ``ts_sub``)."""
        return 0

    def _time_values(self, ts: "np.typing.NDArray[np.int64]") -> list[list[Any]]:
        """Python lists for the time column(s) of a batch (``Any``: pyodbc parameter values)."""
        raise NotImplementedError

    def insert_sql(self) -> str:
        """The parameterised INSERT used by :meth:`_insert`."""
        cols = _INSERT_COLS.format(time=", ".join(self.time_cols))
        marks = ", ".join("?" * (7 + len(self.time_cols)))
        return f"INSERT INTO {self.table} ({cols}) VALUES ({marks})"

    def _insert(self, tables: Sequence[pa.Table], key: int) -> int:
        con = self._conn()
        sql = self.insert_sql()
        n = 0
        con.autocommit = False
        try:
            cur = con.cursor()
            cur.fast_executemany = True
            for t in tables:
                for off in range(0, t.num_rows, BATCH_ROWS):
                    b = t.slice(off, BATCH_ROWS)
                    k = b.num_rows
                    ts = b.column("ts_ns").to_numpy()
                    columns = [
                        [key] * k,
                        *self._time_values(ts),
                        *(b.column(c).to_numpy().tolist() for c in ("price", "size", "side")),
                        *(b.column(c).to_numpy().tolist() for c in ("quality", "ingested_at_ns")),
                        b.column("version").to_numpy().tolist(),
                    ]
                    cur.executemany(sql, list(zip(*columns, strict=True)))
                    con.commit()
                    n += k
        except BaseException:
            con.rollback()  # never leave a half-applied batch behind
            raise
        finally:
            con.autocommit = True
        return n

    def size_bytes(self) -> int:
        """Used pages (data + index + LOB, incl. columnstore) of the table, 8 KiB each."""
        row = (
            self._conn()
            .execute(
                "SELECT COALESCE(SUM(used_page_count), 0) FROM sys.dm_db_partition_stats"
                " WHERE object_id = OBJECT_ID(?)",
                self.table,
            )
            .fetchone()
        )
        return int(row[0]) * PAGE_BYTES if row else 0

    def allocated_bytes(self) -> int:
        """Reserved (allocated) pages of the table x 8 KiB: the at-rest EUR billing basis.

        Azure SQL Database bills data storage on the provisioned maximum data size, not on the
        pages in use, and a database can never be provisioned below what is allocated. Used pages
        (:meth:`size_bytes`) therefore UNDER-state what a SQL format costs at rest; reserved pages
        (whole extents, including the free pages in them) are the tightest basis a single table
        measurement supports. Real bills add system objects, the log and headroom, which are not
        modelled. Blob bills the stored bytes, so for files the runner uses ``size_bytes``.
        """
        row = (
            self._conn()
            .execute(
                "SELECT COALESCE(SUM(reserved_page_count), 0) FROM sys.dm_db_partition_stats"
                " WHERE object_id = OBJECT_ID(?)",
                self.table,
            )
            .fetchone()
        )
        return int(row[0]) * PAGE_BYTES if row else 0

    def reopen(self) -> None:
        """New connection and an empty buffer pool (``CHECKPOINT`` + ``DBCC DROPCLEANBUFFERS``).

        Needs sysadmin. Where it is refused (managed Azure SQL) the pool stays warm, ``cold_mode``
        becomes ``reconnect_only`` and a warning is logged once: such "cold" runs are only a
        fresh connection and must be labelled that way in the report.
        """
        import pyodbc  # noqa: PLC0415 - lazy, see module docstring

        if self._con is not None:
            self._con.close()
        self._con = None
        try:
            con = self._conn()
            con.execute("CHECKPOINT")
            con.execute("DBCC DROPCLEANBUFFERS")
            self.cold_mode = "dropcleanbuffers"
        except pyodbc.Error:
            if self.cold_mode != "reconnect_only":
                log.warning("cold_start_fallback", format=self.id, mode="reconnect_only")
            self.cold_mode = "reconnect_only"

    def teardown(self) -> None:
        """Drop the table and schema, close the connection."""
        if self._created:
            try:
                con = self._conn()
                con.execute(f"DROP TABLE IF EXISTS {self.table}")
                con.execute(f"DROP SCHEMA IF EXISTS [{self.schema}]")
            finally:
                self._created = False
        if self._con is not None:
            self._con.close()
            self._con = None

    # --- workloads --------------------------------------------------------------------------
    def _range(self, lo: int, hi: int) -> tuple[str, list[Any]]:
        """Predicate + params for ``lo <= ts < hi`` (``Any``: str or int parameters)."""
        raise NotImplementedError

    def _scope(self, p: Mapping[str, Any]) -> tuple[str, list[Any]]:  # Any: JSON params
        """Instrument + time scope shared by every workload."""
        keys = [self._keys[i] for i in p["instruments"]]
        lo, hi = (p["lo_ns"], p["hi_ns"]) if p["lo_ns"] is not None else month_range(p["months"])
        pred, args = self._range(lo, hi)
        marks = ", ".join("?" * len(keys))
        return f"instrument_key IN ({marks}) AND {pred}", [*keys, *args]

    def statement(self, workload: str, scope: str) -> str:
        """T-SQL of one workload (pure string; ``scope`` comes from :meth:`_scope`)."""
        t, ns, cols = self.table, self.ns_expr, self.time_cols
        desc = ", ".join(f"{c} DESC" for c in cols)
        if workload == "W2":
            # Daily OHLC as one grouped pass (min/max time, high, low, volume) plus two index
            # seeks per day for open and close. The first formulation used two ROW_NUMBER()
            # windows over the whole month range, which forces a sort on the expression
            # partition; this is what a DBA would write and is no worse for any of F0-F2. The
            # seek ties are broken on the trailing time columns (F0 ``ts_sub``).
            ts0, tie = cols[0], cols[1:] or cols[:1]
            asc, dsc = ", ".join(tie), ", ".join(f"{c} DESC" for c in tie)
            seek = (
                f"(SELECT TOP 1 price FROM {t} WHERE instrument_key = g.k AND {ts0} = g.{{t}}"
                " AND side = 0 AND version = 1 ORDER BY {order})"
            )
            return (
                "SELECT COUNT_BIG(*), SUM(n), SUM(o), SUM(h), SUM(l), SUM(c), SUM(v) FROM ("
                f"SELECT g.n, g.h, g.l, g.v, {seek.format(t='t0', order=asc)} AS o,"
                f" {seek.format(t='t1', order=dsc)} AS c FROM ("
                f"SELECT instrument_key AS k, {self.day_expr} AS d, MIN({ts0}) AS t0,"
                f" MAX({ts0}) AS t1, MAX(price) AS h, MIN(price) AS l,"
                " SUM(CAST(size AS FLOAT)) AS v, COUNT_BIG(*) AS n"
                f" FROM {t} WHERE version = 1 AND side = 0 AND {scope}"
                f" GROUP BY instrument_key, {self.day_expr}) g) x"
            )
        if workload == "W3":
            return (
                f"SELECT COUNT_BIG(*), SUM(price), MIN(price), MAX(price) FROM {t}"
                f" WHERE version = 1 AND {scope}"
            )
        if workload == "W4":
            return (
                f"SELECT TOP 1 {ns}, price FROM {t}"
                f" WHERE version = 1 AND side = 0 AND {scope} ORDER BY {desc}, side DESC"
            )
        if workload == "W5":
            return (
                f"SELECT instrument_key, COUNT_BIG(*), SUM(price) FROM {t}"
                f" WHERE version = 1 AND {scope} GROUP BY instrument_key"
            )
        if workload == "W6":
            part = ", ".join([*cols, "side"])
            return (
                "SELECT COUNT_BIG(*), SUM(price), SUM(CASE WHEN version = 2 THEN 1 ELSE 0 END)"
                " FROM (SELECT price, version, ROW_NUMBER() OVER"
                f" (PARTITION BY {part} ORDER BY version DESC) AS rn FROM {t}"
                f" WHERE ingested_at_ns <= ? AND {scope}) x WHERE rn = 1"
            )
        raise ConfigError(f"{self.id} has no workload {workload!r}")

    def _query(
        self, sql: str, args: Sequence[Any], *, io: bool = False
    ) -> tuple[list[Any], int]:  # Any: pyodbc rows / params
        """Run one statement; with ``io`` also return pages read from ``STATISTICS IO``."""
        con = self._conn()
        cur = con.cursor()
        if io:
            cur.execute("SET STATISTICS IO ON")
        try:
            cur.execute(sql, *args)
            rows = cur.fetchall()
            msgs = list(getattr(cur, "messages", None) or [])
            while io and cur.nextset():
                msgs += list(getattr(cur, "messages", None) or [])
        finally:
            if io:
                cur.execute("SET STATISTICS IO OFF")
        return rows, parse_io_pages(msgs) if io else 0

    def _execute(self, workload: str, p: Mapping[str, Any], *, io: bool) -> tuple[Value, int]:
        scope, args = self._scope(p)
        sql = self.statement(workload, scope)
        if workload == "W6":
            out: Value = {}
            total = 0
            for k, asof in enumerate(p["asof_ns"]):
                rows, pages = self._query(sql, [asof, *args], io=io)
                r = rows[0]
                out[f"n_{k}"] = int(r[0])
                out[f"sum_price_{k}"] = float(r[1] or 0.0)
                out[f"n_restated_{k}"] = int(r[2] or 0)
                total += pages
            return out, total
        rows, pages = self._query(sql, args, io=io)
        r = rows[0]
        if workload == "W2":
            keys = ("n_days", "n_trades", "sum_open", "sum_high", "sum_low", "sum_close")
            v: Value = {keys[0]: int(r[0]), keys[1]: int(r[1])}
            v.update({k: float(x) for k, x in zip(keys[2:], r[2:6], strict=True)})
            v["sum_volume"] = float(r[6])
            return v, pages
        if workload == "W3":
            return {
                "n": int(r[0]),
                "sum_price": float(r[1] or 0.0),
                "min_price": float(r[2] or 0.0),
                "max_price": float(r[3] or 0.0),
            }, pages
        if workload == "W4":
            return {"ts_ns": int(r[0]), "price": float(r[1])}, pages
        got = {int(x[0]): (int(x[1]), float(x[2])) for x in rows}  # W5
        out = {}
        for inst in p["instruments"]:
            n, s = got.get(self._keys[inst], (0, 0.0))
            out[f"n:{inst}"], out[f"sum_price:{inst}"] = n, s
        return out, pages

    def run(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON params
        """Run W2-W6 (value only; no profiling in the timed call)."""
        value, _ = self._execute(workload, params, io=False)
        return WorkloadResult(value)

    def io_profile(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON
        """Untimed pass with ``SET STATISTICS IO ON``: bytes read = (logical + lob) pages x 8 KiB.

        Logical reads count pages touched regardless of buffer-pool state, so this is the
        engine-independent analogue of DuckDB's bytes read. Rows scanned are not reported.
        """
        value, pages = self._execute(workload, params, io=True)
        return WorkloadResult(value, None, pages * PAGE_BYTES, "sql_statistics_io")


__all__ = ["SqlFormat", "SqlTarget", "dt2_text", "month_range", "parse_io_pages"]
