"""SQL formats F0-F2: pure helpers, DDL design and registry (no database; T-202)."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
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

TARGET = SqlTarget("127.0.0.1,1433", "db", "sa", "not-a-real-password")


def test_registry_has_sql_formats_in_runner() -> None:
    assert {"F0", "F1", "F2", "F5"} <= set(FORMATS)
    assert set(SQL_FORMATS) == {"F0", "F1", "F2"}
    assert [SQL_FORMATS[k].id for k in sorted(SQL_FORMATS)] == ["F0", "F1", "F2"]


def test_missing_env_raises_without_leaking(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert (t.server, t.database, t.user) == ("h,1", "d", "sa")
    assert "s3cret-value" not in repr(t).replace("password='s3cret-value'", "")


def test_design_levers_isolate_time_type_and_compression() -> None:
    f0, f1, f2 = (c(Path("."), TARGET) for c in (SqlF0, SqlF1, SqlF2))
    assert "DATETIME2(7)" in f0.ddl
    assert "DATA_COMPRESSION = NONE" in f0.ddl
    assert "ts_ns BIGINT" in f1.ddl
    assert "DATA_COMPRESSION = PAGE" in f1.ddl
    assert "PRIMARY KEY CLUSTERED" in f1.ddl
    assert "CLUSTERED COLUMNSTORE" in f2.ddl
    assert "ts_ns BIGINT" in f2.ddl
    assert "PRIMARY KEY" not in f2.ddl
    assert "COLUMNSTORE" not in f1.ddl
    assert f2.post_load
    assert "REBUILD" in f2.post_load[0]
    assert len({f.table for f in (f0, f1, f2)}) == 3
    assert all(f.storage_price_key == "sql_gp_storage_gb_month" for f in (f0, f1, f2))


def test_f0_ts_columns_are_exact_for_ns_resolution() -> None:
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


@given(st.lists(st.integers(0, 4_102_444_800 * 10**9), min_size=1, max_size=20))
def test_dt2_text_floors_to_100ns_and_roundtrips(values: list[int]) -> None:
    ts = np.array(values, dtype=np.int64)
    out = dt2_text(ts)
    for s, v in zip(out, values, strict=True):
        head, frac = s.split(".")
        assert len(frac) == 7
        sec = int(datetime.fromisoformat(head).replace(tzinfo=UTC).timestamp())
        assert sec * 10**9 + int(frac) * 100 == v - v % 100


def test_month_range_handles_year_end() -> None:
    lo, hi = month_range([[2024, 11], [2024, 12]])
    assert lo == int(datetime(2024, 11, 1, tzinfo=UTC).timestamp()) * 10**9
    assert hi == int(datetime(2025, 1, 1, tzinfo=UTC).timestamp()) * 10**9


def test_parse_io_pages_counts_logical_and_lob_not_physical() -> None:
    msg = (
        "01000",
        "Table 'tick_f1'. Scan count 1, logical reads 10, physical reads 3, page server reads 0,"
        " read-ahead reads 4, page server read-ahead reads 0, lob logical reads 5, lob physical"
        " reads 1, lob page server reads 0, lob read-ahead reads 0.",
    )
    assert parse_io_pages([msg, msg]) == 30
    assert parse_io_pages([]) == 0


def test_base_is_abstract_about_time_handling() -> None:
    with pytest.raises(NotImplementedError):
        SqlFormat._range(SqlF1(Path("."), TARGET), 0, 1)
