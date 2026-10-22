"""F0-F2 against a real SQL Server (T-202): every workload equals the polars oracle and F5.

Needs ``TIDAL_TEST_SQL_URL`` (``host,port``) and ``TIDAL_TEST_SQL_PASSWORD`` /
``TIDAL_SQL__PASSWORD``; a throwaway database is created and dropped.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from benchmarks.storage.formats import SQL_FORMATS
from benchmarks.storage.formats.sql_base import SqlTarget
from benchmarks.storage.run import F5Format, run_benchmark
from benchmarks.storage.workloads import (
    WORKLOADS,
    Dataset,
    DatasetSpec,
    build_dataset,
    values_match,
)

pytestmark = pytest.mark.integration

_SIZES: dict[str, int] = {}  # filled by the parametrised test, read by the F5 comparison
SPEC = DatasetSpec(
    scale="S", seed=7, start=date(2024, 12, 1), end=date(2024, 12, 31), n_instruments=2
)


@pytest.fixture(scope="module")
def target() -> Iterator[SqlTarget]:
    """A throwaway database on the shared SQL Server."""
    server = os.environ.get("TIDAL_TEST_SQL_URL")
    pw = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get("TIDAL_SQL__PASSWORD")
    if not (server and pw):
        pytest.skip("TIDAL_TEST_SQL_URL and a SQL password are required")
    admin = SqlTarget(server, "master", os.environ.get("TIDAL_SQL__USER", "sa"), pw)
    db = f"tidal_bench_test_{uuid.uuid4().hex[:10]}"
    con = admin.connect()
    con.execute(f"CREATE DATABASE [{db}]")
    try:
        yield SqlTarget(server, db, admin.user, pw)
    finally:
        con.execute(f"ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        con.execute(f"DROP DATABASE [{db}]")
        con.close()


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    return build_dataset(SPEC, tmp_path_factory.mktemp("bench-sql-data"))


@pytest.mark.parametrize("fid", ["F0", "F1", "F2"])
def test_format_matches_oracle_on_all_workloads(
    fid: str, target: SqlTarget, dataset: Dataset, tmp_path: Path
) -> None:
    fmt = SQL_FORMATS[fid](tmp_path, target)
    assert fmt.id == fid
    stats = fmt.load(dataset)
    assert stats.rows > sum(dataset.rows.values())  # base ticks plus the restatements
    assert stats.bytes_written == fmt.size_bytes() > 0
    for wid in ("W2", "W3", "W4", "W5", "W6"):
        p = WORKLOADS[wid].params(dataset)
        want = WORKLOADS[wid].oracle(dataset, p)
        assert values_match(fmt.run(wid, p).value, want), (fid, wid)
        io = fmt.io_profile(wid, p)
        assert values_match(io.value, want), (fid, wid, "profiled")
        assert io.bytes_read is not None
        assert io.bytes_read > 0
        assert io.bytes_read_source == "sql_statistics_io"
    fmt.reopen()  # cold start must keep working after the pool is dropped
    p = WORKLOADS["W2"].params(dataset)
    assert values_match(fmt.run("W2", p).value, WORKLOADS["W2"].oracle(dataset, p))
    _SIZES[fid] = fmt.size_bytes()
    fmt.teardown()


def test_sizes_order_and_f5_comparison(dataset: Dataset, tmp_path: Path) -> None:
    """Runs after the parametrised test: PAGE+BIGINT < DATETIME2, columnstore < rowstore."""
    assert set(_SIZES) == {"F0", "F1", "F2"}
    assert _SIZES["F1"] < _SIZES["F0"]
    assert _SIZES["F2"] < _SIZES["F1"]
    f5 = F5Format(tmp_path / "f5")
    f5.load(dataset)
    assert f5.size_bytes() < _SIZES["F2"]  # sanity only; the claim is made in T-204
    f5.teardown()


def test_runner_end_to_end_with_sql_formats(
    target: SqlTarget, dataset: Dataset, tmp_path: Path
) -> None:
    rows = run_benchmark(
        SPEC, ["F1"], ["W1", "W2", "W4"], 1, tmp_path / "out",
        data_root=dataset.root.parent,
        factories={"F1": lambda w: SQL_FORMATS["F1"](w, target)},
    )  # fmt: skip
    assert {r.workload_id for r in rows} == {"W1", "W2", "W4"}
    assert all(r.bytes_read for r in rows if r.workload_id != "W1")
