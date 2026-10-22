"""SQL formats F0-F2: pure helpers, DDL design, golden SQL and registry (no database; T-202)."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from benchmarks.storage.formats import SQL_FORMATS
from benchmarks.storage.formats.sql_base import (
    SqlFormat,
    SqlTarget,
    dt2_text,
    month_range,
    parse_io_pages,
)
from benchmarks.storage.formats.sql_f0 import SqlF0
from benchmarks.storage.formats.sql_f1 import SqlF1
from benchmarks.storage.formats.sql_f2 import SqlF2
from benchmarks.storage.run import FORMATS
from hypothesis import given
from hypothesis import strategies as st

from tidal.common.errors import ConfigError

SECRET = "not-a-real-password"  # noqa: S105 - test value
TARGET = SqlTarget("127.0.0.1,1433", "db", "sa", SECRET)


def _fmt(cls: type[SqlFormat]) -> SqlFormat:
    f = cls(Path("."), TARGET)
    f.table = f"[s].[tick_{f.id.lower()}]"
    f._keys = {"A": 1}
    return f


def test_registry_has_sql_formats_in_runner() -> None:
    assert {"F0", "F1", "F2", "F5"} <= set(FORMATS)
    assert set(SQL_FORMATS) == {"F0", "F1", "F2"}
    assert [SQL_FORMATS[k].id for k in sorted(SQL_FORMATS)] == ["F0", "F1", "F2"]


def test_missing_env_raises_and_password_stays_out_of_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in (
        "TIDAL_TEST_SQL_URL",
        "TIDAL_SQL__SERVER",
        "TIDAL_TEST_SQL_PASSWORD",
        "TIDAL_SQL__PASSWORD",
        "TIDAL_SQL__DATABASE",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError):
        SqlF1(Path("."))
    monkeypatch.setenv("TIDAL_TEST_SQL_URL", "h,1")
    monkeypatch.setenv("TIDAL_SQL__DATABASE", "d")
    monkeypatch.setenv("TIDAL_SQL__PASSWORD", "s3cret-value")
    t = SqlTarget.from_env()
    assert (t.server, t.database, t.user, t.password) == ("h,1", "d", "sa", "s3cret-value")
    assert "s3cret-value" not in repr(t)
    assert "s3cret-value" not in str(t)


def test_odbc_string_escapes_braces_and_trusts_only_loopback() -> None:
    local = SqlTarget("127.0.0.1,1433", "d", "sa", "pa}ss{w")
    s = local.odbc_string("ODBC Driver 18 for SQL Server")
    assert "PWD={pa}}ss{w}" in s
    assert "TrustServerCertificate=yes" in s
    assert "TrustServerCertificate=yes" in SqlTarget("localhost", "d", "u", "p").odbc_string("x")
    remote = SqlTarget("tidal.database.windows.net", "d", "u", "p")
    assert remote.odbc_string("x").endswith("TrustServerCertificate=no")
    assert not remote.is_local


def test_importing_the_formats_does_not_need_pyodbc() -> None:
    code = (
        "import sys; sys.modules['pyodbc'] = None;"
        "import benchmarks.storage.formats, benchmarks.storage.run"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr[-400:]


def test_design_levers_isolate_time_type_and_compression() -> None:
    f0, f1, f2 = (c(Path("."), TARGET) for c in (SqlF0, SqlF1, SqlF2))
    d0, d1, d2 = " ".join(f0.ddl), " ".join(f1.ddl), " ".join(f2.ddl)
    assert "DATETIME2(7)" in d0
    assert "DATA_COMPRESSION = NONE" in d0
    assert "ts_ns BIGINT" in d1
    assert "DATA_COMPRESSION = PAGE" in d1
    assert "PRIMARY KEY CLUSTERED" in d1
    assert "CLUSTERED COLUMNSTORE INDEX cci ON {t} ORDER (instrument_key, ts_ns)" in d2
    assert "PRIMARY KEY" not in d2
    assert "COLUMNSTORE" not in d1
    assert "REBUILD" in f2.post_load[0]
    assert f0.post_load == f1.post_load  # symmetric post-load rebuild for the rowstores
    assert "REBUILD" in f0.post_load[0]
    assert len({f.table for f in (f0, f1, f2)}) == 3
    assert all(f.storage_price_key == "sql_gp_storage_gb_month" for f in (f0, f1, f2))


def test_golden_scope_and_insert_sql() -> None:
    f0, f1 = _fmt(SqlF0), _fmt(SqlF1)
    p: dict[str, Any] = {"instruments": ["A"], "months": [[2024, 12]], "lo_ns": None, "hi_ns": None}
    assert f1._scope(p) == (
        "instrument_key IN (?) AND ts_ns >= ? AND ts_ns < ?",
        [1, 1733011200000000000, 1735689600000000000],
    )
    assert f0._scope(p) == (
        "instrument_key IN (?) AND ts >= CAST(? AS DATETIME2(7)) AND ts < CAST(? AS DATETIME2(7))",
        [1, "2024-12-01 00:00:00.0000000", "2025-01-01 00:00:00.0000000"],
    )
    assert f1.insert_sql() == (
        "INSERT INTO [s].[tick_f1] (instrument_key, ts_ns, price, size, side, quality,"
        " ingested_at_ns, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    assert f0.insert_sql().startswith(
        "INSERT INTO [s].[tick_f0] (instrument_key, ts, ts_sub, price"
    )
    assert f0.insert_sql().count("?") == 9


def test_golden_workload_statements() -> None:
    f0, f1 = _fmt(SqlF0), _fmt(SqlF1)
    assert f1.statement("W3", "SC") == (
        "SELECT COUNT_BIG(*), SUM(price), MIN(price), MAX(price) FROM [s].[tick_f1]"
        " WHERE version = 1 AND SC"
    )
    assert f1.statement("W4", "SC") == (
        "SELECT TOP 1 ts_ns, price FROM [s].[tick_f1]"
        " WHERE version = 1 AND side = 0 AND SC ORDER BY ts_ns DESC, side DESC"
    )
    assert f0.statement("W4", "SC") == (
        "SELECT TOP 1 DATEDIFF_BIG(NANOSECOND, '1970-01-01', ts) + ts_sub, price"
        " FROM [s].[tick_f0] WHERE version = 1 AND side = 0 AND SC"
        " ORDER BY ts DESC, ts_sub DESC, side DESC"
    )
    assert f1.statement("W5", "SC") == (
        "SELECT instrument_key, COUNT_BIG(*), SUM(price) FROM [s].[tick_f1]"
        " WHERE version = 1 AND SC GROUP BY instrument_key"
    )
    assert f0.statement("W6", "SC") == (
        "SELECT COUNT_BIG(*), SUM(price), SUM(CASE WHEN version = 2 THEN 1 ELSE 0 END)"
        " FROM (SELECT price, version, ROW_NUMBER() OVER (PARTITION BY ts, ts_sub, side"
        " ORDER BY version DESC) AS rn FROM [s].[tick_f0]"
        " WHERE ingested_at_ns <= ? AND SC) x WHERE rn = 1"
    )
    assert f1.statement("W2", "SC") == (
        "SELECT COUNT_BIG(*), SUM(n), SUM(o), SUM(h), SUM(l), SUM(c), SUM(v) FROM ("
        "SELECT g.n, g.h, g.l, g.v, (SELECT TOP 1 price FROM [s].[tick_f1]"
        " WHERE instrument_key = g.k AND ts_ns = g.t0 AND side = 0 AND version = 1"
        " ORDER BY ts_ns) AS o, (SELECT TOP 1 price FROM [s].[tick_f1]"
        " WHERE instrument_key = g.k AND ts_ns = g.t1 AND side = 0 AND version = 1"
        " ORDER BY ts_ns DESC) AS c FROM (SELECT instrument_key AS k,"
        " ts_ns / CAST(86400000000000 AS BIGINT) AS d, MIN(ts_ns) AS t0, MAX(ts_ns) AS t1,"
        " MAX(price) AS h, MIN(price) AS l, SUM(CAST(size AS FLOAT)) AS v, COUNT_BIG(*) AS n"
        " FROM [s].[tick_f1] WHERE version = 1 AND side = 0 AND SC"
        " GROUP BY instrument_key, ts_ns / CAST(86400000000000 AS BIGINT)) g) x"
    )
    w2 = f0.statement("W2", "SC")
    assert "ORDER BY ts_sub) AS o" in w2  # F0 open/close ties broken on the sub-100 ns remainder
    assert "ORDER BY ts_sub DESC) AS c" in w2
    assert "CAST(ts AS DATE) AS d" in w2
    with pytest.raises(ConfigError):
        f1.statement("W1", "SC")


def test_f0_ts_columns_are_exact_for_ns_resolution_and_size_correction() -> None:
    ts = np.array([1_700_000_000_123_456_789, 0, 86_399_999_999_999], dtype=np.int64)
    assert dt2_text(ts) == [
        "2023-11-14 22:13:20.1234567",
        "1970-01-01 00:00:00.0000000",
        "1970-01-01 23:59:59.9999999",
    ]
    f0 = SqlF0(Path("."), TARGET)
    _text, sub = f0._time_values(ts)
    assert sub == [89, 0, 99]
    with pytest.raises(ConfigError):
        f0._range(1, 200)
    f0.rows_loaded = 1234
    assert f0.size_correction_bytes() == 1234
    assert SqlF1(Path("."), TARGET).size_correction_bytes() == 0


@given(st.lists(st.integers(0, 4_102_444_800 * 10**9), min_size=1, max_size=20))
def test_dt2_text_floors_to_100ns_and_roundtrips(values: list[int]) -> None:
    ts = np.array(values, dtype=np.int64)
    for s, v in zip(dt2_text(ts), values, strict=True):
        head, frac = s.split(".")
        assert len(frac) == 7
        sec = int(datetime.fromisoformat(head).replace(tzinfo=UTC).timestamp())
        assert sec * 10**9 + int(frac) * 100 == v - v % 100


def test_month_range_handles_year_end() -> None:
    lo, hi = month_range([[2024, 11], [2024, 12]])
    assert lo == int(datetime(2024, 11, 1, tzinfo=UTC).timestamp()) * 10**9
    assert hi == int(datetime(2025, 1, 1, tzinfo=UTC).timestamp()) * 10**9


def test_parse_io_pages_counts_only_the_tick_table() -> None:
    tick = (
        "01000",
        "Table 'tick_f1'. Scan count 1, logical reads 10, physical reads 3, page server reads 0,"
        " read-ahead reads 4, page server read-ahead reads 0, lob logical reads 5, lob physical"
        " reads 1, lob page server reads 0, lob read-ahead reads 0.",
    )
    spill = (
        "01000",
        "Table 'Worktable'. Scan count 0, logical reads 999, physical reads 0, lob logical reads 7."
        "\nTable 'Workfile'. Scan count 0, logical reads 888, physical reads 0."
        "\nTable 'tick_f2'. Scan count 1, logical reads 1, physical reads 0, lob logical reads 2.",
    )
    assert parse_io_pages([tick, tick]) == 30
    prefixed = (
        "01000",
        "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]" + tick[1],
    )
    assert parse_io_pages([prefixed]) == 15  # pyodbc prepends the driver tag
    assert parse_io_pages([spill]) == 3  # only tick_f2: 1 logical + 2 lob
    assert parse_io_pages([("01000", "Table 'Worktable'. Scan count 1, logical reads 5")]) == 0
    assert parse_io_pages([]) == 0


def test_base_is_abstract_about_time_handling() -> None:
    with pytest.raises(NotImplementedError):
        SqlFormat._range(SqlF1(Path("."), TARGET), 0, 1)


def test_insert_failure_rolls_back_and_restores_autocommit() -> None:
    class Cur:
        fast_executemany = False

        def executemany(self, *_a: object) -> None:
            raise RuntimeError("boom")

    class Con:
        autocommit = True
        rolled = False

        def cursor(self) -> Cur:
            return Cur()

        def rollback(self) -> None:
            self.rolled = True

        def commit(self) -> None:
            pytest.fail("must not commit a failed batch")

    f = _fmt(SqlF1)
    con = Con()
    f._con = con
    t = pa.table(
        {
            "ts_ns": pa.array([1], pa.int64()),
            "price": [1.0],
            "size": pa.array([1.0], pa.float32()),
            "side": pa.array([0], pa.int8()),
            "quality": pa.array([0], pa.int8()),
            "ingested_at_ns": pa.array([1], pa.int64()),
            "version": pa.array([1], pa.int16()),
        }
    )
    with pytest.raises(RuntimeError):
        f._insert([t], 1)
    assert con.rolled
    assert con.autocommit is True


def test_cold_mode_defaults_to_dropcleanbuffers() -> None:
    assert SqlF1(Path("."), TARGET).cold_mode == "dropcleanbuffers"
