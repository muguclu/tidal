"""Shared machinery for the SQL Server candidates F0-F2 (SPEC-02 section 2).

All three store the same tick table as ``md.tick_hot`` (one table per format, one throwaway schema
per format instance) and answer W2-W6 with T-SQL; only the physical design differs:

* F0 ``DATETIME2(7)`` time column, no compression (the naive baseline),
* F1 ``ts_ns BIGINT``, PAGE compression, clustered PK (the tuned rowstore),
* F2 ``ts_ns BIGINT`` in a clustered columnstore index.

Connection: pyodbc directly (``fast_executemany`` for loads, ``STATISTICS IO`` messages for the
bytes-read metric). Every Azure/SQL touchpoint takes a :class:`SqlTarget`, so tests point it at a
throwaway database. Nothing here is imported unless a SQL format is built.
"""

import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final

import numpy as np
import polars as pl
import pyarrow as pa
import pyodbc

from benchmarks.storage.workloads import Dataset, LoadStats, Value, WorkloadResult
from tidal.common.errors import ConfigError

PAGE_BYTES: Final = 8192
BATCH_ROWS: Final = 100_000  # rows per fast_executemany call (and per commit)
NS_DAY: Final = 86_400 * 10**9
_INSERT_COLS: Final = "instrument_key, {time}, price, size, side, quality, ingested_at_ns, version"
_LOGICAL = re.compile(r"(?<!lob )logical reads (\d+)")
_LOB_LOGICAL = re.compile(r"lob logical reads (\d+)")


@dataclass(frozen=True)
class SqlTarget:
    """Where a SQL format runs: ``server`` is the ODBC form (``host,port``)."""

    server: str
    database: str
    user: str
    password: str

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

    def connect(self, *, database: str | None = None) -> pyodbc.Connection:
        """Open an autocommit connection (newest installed SQL Server ODBC driver)."""
        drivers = sorted(
            (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
            key=lambda d: int(d.split()[2]),
        )
        if not drivers:
            raise ConfigError("no 'ODBC Driver N for SQL Server' installed")
        conn = (
            f"DRIVER={{{drivers[-1]}}};SERVER={self.server};DATABASE={database or self.database};"
            f"UID={self.user};PWD={{{self.password}}};TrustServerCertificate=yes"
        )
        return pyodbc.connect(conn, autocommit=True, timeout=30)


def dt2_text(ts_ns: "np.typing.NDArray[np.int64]") -> list[str]:
    """Epoch ns -> ``YYYY-MM-DD HH:MM:SS.fffffff`` (floored to the 100 ns DATETIME2(7) tick)."""
    s = pl.Series(ts_ns).cast(pl.Datetime("ns", "UTC"))
    return s.dt.to_string("%Y-%m-%d %H:%M:%S%.9f").str.slice(0, 27).to_list()


def month_range(months: Sequence[Sequence[int]]) -> tuple[int, int]:
    """``[lo_ns, hi_ns)`` covering the first through the last (year, month)."""
    ms = sorted((int(y), int(m)) for y, m in months)
    (y0, m0), (y1, m1) = ms[0], ms[-1]
    lo = datetime(y0, m0, 1, tzinfo=UTC)
    hi = datetime(y1 + (m1 == 12), m1 % 12 + 1, 1, tzinfo=UTC)
    return int(lo.timestamp()) * 10**9, int(hi.timestamp()) * 10**9


def parse_io_pages(messages: Sequence[tuple[str, str]]) -> int:
    """Pages read according to ``SET STATISTICS IO`` messages (logical + lob logical reads)."""
    pages = 0
    for _state, text in messages:
        pages += sum(int(x) for x in _LOGICAL.findall(text))
        pages += sum(int(x) for x in _LOB_LOGICAL.findall(text))
    return pages


class SqlFormat:
    """Base for F0-F2; subclasses set the time representation and the physical design."""

    id: str
    storage_price_key: str = "sql_gp_storage_gb_month"
    time_cols: ClassVar[tuple[str, ...]]  # columns that hold the timestamp, in key order
    ns_expr: ClassVar[str]  # T-SQL expression: exact epoch ns of a row
    day_expr: ClassVar[str]  # T-SQL expression: UTC day bucket of a row
    ddl: ClassVar[str]  # CREATE TABLE with {t} (qualified name) and {pk} (constraint name)
    post_load: ClassVar[tuple[str, ...]] = ()  # statements run after the last insert (with {t})

    def __init__(self, workdir: Path, target: SqlTarget | None = None) -> None:
        """``workdir`` is unused (state lives in SQL Server); ``target`` defaults to the env."""
        self.workdir = workdir
        self.target = target or SqlTarget.from_env()
        self.schema = f"bench_{uuid.uuid4().hex[:10]}"
        self.table = f"[{self.schema}].[tick_{self.id.lower()}]"
        self._con: pyodbc.Connection | None = None
        self._keys: dict[str, int] = {}
        self._created = False

    # --- connection -------------------------------------------------------------------------
    def _conn(self) -> pyodbc.Connection:
        if self._con is None:
            self._con = self.target.connect()
        return self._con

    def _create(self) -> None:
        con = self._conn()
        con.execute(f"CREATE SCHEMA [{self.schema}]")
        con.execute(self.ddl.format(t=self.table, pk=f"pk_{self.schema}"))
        self._created = True

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
        return LoadStats(total, self.size_bytes())

    def _time_values(self, ts: "np.typing.NDArray[np.int64]") -> list[list[Any]]:
        """Python lists for the time column(s) of a batch (``Any``: pyodbc parameter values)."""
        raise NotImplementedError

    def _insert(self, tables: Sequence[pa.Table], key: int) -> int:
        con = self._conn()
        cols = _INSERT_COLS.format(time=", ".join(self.time_cols))
        marks = ", ".join("?" * (7 + len(self.time_cols)))
        sql = f"INSERT INTO {self.table} ({cols}) VALUES ({marks})"  # noqa: S608
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

    def reopen(self) -> None:
        """New connection and an empty buffer pool (``CHECKPOINT`` + ``DBCC DROPCLEANBUFFERS``).

        Needs sysadmin; where it is refused (managed Azure SQL) the pool stays warm and cold
        equals a fresh connection only.
        """
        if self._con is not None:
            self._con.close()
        self._con = None
        try:
            con = self._conn()
            con.execute("CHECKPOINT")
            con.execute("DBCC DROPCLEANBUFFERS")
        except pyodbc.Error:
            pass

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
        t, ns = self.table, self.ns_expr
        scope, args = self._scope(p)
        order = ", ".join(self.time_cols)
        if workload == "W2":
            day = self.day_expr
            sql = (
                "SELECT COUNT_BIG(*), SUM(n), SUM(o), SUM(h), SUM(l), SUM(c), SUM(v) FROM ("  # noqa: S608
                f"SELECT d, MAX(CASE WHEN ra = 1 THEN price END) o, MAX(price) h, MIN(price) l,"
                " MAX(CASE WHEN rd = 1 THEN price END) c, SUM(CAST(size AS FLOAT)) v,"
                " COUNT_BIG(*) n FROM ("
                f"SELECT {day} AS d, price, size,"
                f" ROW_NUMBER() OVER (PARTITION BY {day} ORDER BY {order}) ra,"
                f" ROW_NUMBER() OVER (PARTITION BY {day} ORDER BY "
                + ", ".join(f"{c} DESC" for c in self.time_cols)
                + f") rd FROM {t} WHERE version = 1 AND side = 0 AND {scope}) x GROUP BY d) y"
            )
            rows, pages = self._query(sql, args, io=io)
            r = rows[0]
            keys = ("n_days", "n_trades", "sum_open", "sum_high", "sum_low", "sum_close")
            v: Value = {keys[0]: int(r[0]), keys[1]: int(r[1])}
            v.update({k: float(x) for k, x in zip(keys[2:], r[2:6], strict=True)})
            v["sum_volume"] = float(r[6])
            return v, pages
        if workload == "W3":
            sql = (
                "SELECT COUNT_BIG(*), SUM(price), MIN(price), MAX(price)"  # noqa: S608
                f" FROM {t} WHERE version = 1 AND {scope}"
            )
            rows, pages = self._query(sql, args, io=io)
            r = rows[0]
            n = int(r[0])
            return {
                "n": n,
                "sum_price": float(r[1] or 0.0),
                "min_price": float(r[2] or 0.0),
                "max_price": float(r[3] or 0.0),
            }, pages
        if workload == "W4":
            sql = (
                f"SELECT TOP 1 {ns}, price FROM {t}"  # noqa: S608
                f" WHERE version = 1 AND side = 0 AND {scope}"
                " ORDER BY " + ", ".join(f"{c} DESC" for c in self.time_cols) + ", side DESC"
            )
            rows, pages = self._query(sql, args, io=io)
            return {"ts_ns": int(rows[0][0]), "price": float(rows[0][1])}, pages
        if workload == "W5":
            sql = (
                "SELECT instrument_key, COUNT_BIG(*), SUM(price)"  # noqa: S608
                f" FROM {t} WHERE version = 1 AND {scope} GROUP BY instrument_key"
            )
            rows, pages = self._query(sql, args, io=io)
            got = {int(r[0]): (int(r[1]), float(r[2])) for r in rows}
            out: Value = {}
            for inst in p["instruments"]:
                n, s = got.get(self._keys[inst], (0, 0.0))
                out[f"n:{inst}"], out[f"sum_price:{inst}"] = n, s
            return out, pages
        if workload == "W6":
            return self._w6(p, scope, args, io=io)
        raise ConfigError(f"{self.id} has no workload {workload!r}")

    def _w6(
        self, p: Mapping[str, Any], scope: str, args: list[Any], *, io: bool
    ) -> tuple[Value, int]:  # Any: JSON params / pyodbc params
        part = ", ".join([*self.time_cols, "side"])
        sql = (
            "SELECT COUNT_BIG(*), SUM(price), SUM(CASE WHEN version = 2 THEN 1 ELSE 0 END) FROM ("  # noqa: S608
            f"SELECT price, version, ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY version DESC)"
            f" rn FROM {self.table} WHERE ingested_at_ns <= ? AND {scope}) x WHERE rn = 1"
        )
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
