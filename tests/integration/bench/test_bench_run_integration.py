"""Runner v2 against the compose stack (T-204): Blob mode columns, SQL columns, stale sweeps.

Needs Azurite (``TIDAL_STORAGE__CONNECTION_STRING``) and SQL Server (``TIDAL_TEST_SQL_URL``
= ``host,port`` plus a SQL password). Every container, schema and database a test creates is
removed in a finaliser. The sweeps are restricted to the objects the test created, so a run in
another shell is never touched.
"""

import contextlib
import os
import uuid
from collections.abc import Iterator
from datetime import date
from functools import partial
from pathlib import Path

import pytest
from azure.storage.blob import BlobServiceClient
from benchmarks.storage.formats.sql_base import SqlTarget
from benchmarks.storage.formats.sql_f0 import SqlF0
from benchmarks.storage.results_schema import read_results
from benchmarks.storage.run import (
    main,
    run_benchmark,
    sweep_stale_containers,
    sweep_stale_schemas,
)
from benchmarks.storage.workloads import Dataset, DatasetSpec, build_dataset

from tidal.common.timeutil import SystemClock

pytestmark = pytest.mark.integration

SPEC = DatasetSpec(
    scale="S", seed=7, start=date(2024, 12, 1), end=date(2024, 12, 31), n_instruments=2
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    return build_dataset(SPEC, tmp_path_factory.mktemp("bench-run-data"))


@pytest.fixture
def service() -> BlobServiceClient:
    cs = os.environ.get("TIDAL_STORAGE__CONNECTION_STRING")
    if not cs:
        pytest.skip("TIDAL_STORAGE__CONNECTION_STRING (Azurite) is required")
    return BlobServiceClient.from_connection_string(cs)


@pytest.fixture
def sql_target() -> Iterator[SqlTarget]:
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


def test_azurite_target_records_requests_and_egress(
    dataset: Dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: BlobServiceClient
) -> None:
    monkeypatch.setenv("TIDAL_ENV", "local")
    out = tmp_path / "o"
    main([
        "--scale", "S", "--seed", "7", "--start", "2024-12-01", "--end", "2024-12-31",
        "--instruments", "2", "--formats", "F4,F5", "--workloads", "W2..W5", "--runs", "1",
        "--target", "azurite", "--data-root", str(dataset.root.parent), "--out", str(out),
        "--no-sweep",
    ])  # fmt: skip
    rows, _ = read_results(out / "results.parquet")
    q = [r for r in rows if r.workload_id != "W1"]
    assert q
    assert {r.target for r in rows} == {"azurite"}
    for r in q:
        assert r.requests_get
        assert r.requests_get > 0
        assert r.requests_list
        assert r.requests_list > 0
        assert r.egress_bytes == r.bytes_read  # the Blob "bytes read" IS the wire bytes
        assert r.bytes_read_source == "azurite_http_response_bytes"
        assert r.row_groups_files
    assert not list(service.list_containers(name_starts_with="bench-f5-"))  # torn down


def test_sql_rows_report_na_adjusted_f0_and_billed_basis(
    dataset: Dataset, tmp_path: Path, sql_target: SqlTarget
) -> None:
    rows = run_benchmark(
        dataset.spec, ["F0"], ["W1", "W3"], 1, tmp_path, data_root=dataset.root.parent,
        factories={"F0": partial(SqlF0, target=sql_target)},
    )  # fmt: skip
    w3 = next(r for r in rows if r.workload_id == "W3" and r.cache == "cold")
    assert w3.target == "sql_server"
    assert w3.rows is None
    assert w3.rows_per_s is None
    assert w3.cold_mode == "dropcleanbuffers"
    assert w3.bytes_read_source == "sql_statistics_io"
    assert w3.bytes_at_rest_adjusted is not None
    assert w3.bytes_at_rest_adjusted < w3.bytes_at_rest
    assert w3.bytes_billed is not None
    assert w3.bytes_billed >= w3.bytes_at_rest  # allocated >= used
    warm = next(r for r in rows if r.workload_id == "W3" and r.cache == "warm")
    assert warm.cold_mode == "n/a"


def test_sweeps_remove_only_named_stale_objects(
    service: BlobServiceClient, sql_target: SqlTarget
) -> None:
    name = f"bench-sweeptest-{uuid.uuid4().hex[:8]}"
    service.create_container(name)
    schema = f"bench_{uuid.uuid4().hex[:10]}"
    con = sql_target.connect()
    try:
        con.execute(f"CREATE SCHEMA [{schema}]")
        con.execute(f"CREATE TABLE [{schema}].[tick_f0] (a INT)")
        # a young object survives a 60-minute sweep ...
        assert sweep_stale_schemas(sql_target, min_age_min=60, only={schema}) == []
        later = SystemClock().now().replace(microsecond=0)
        assert sweep_stale_containers(service, min_age_min=60, now=later, prefix=name) == []
        # ... and is dropped once it counts as old (age 0).
        assert sweep_stale_schemas(sql_target, min_age_min=0, only={schema}) == [schema]
        gone = sweep_stale_containers(service, min_age_min=0, prefix=name)
        assert gone == [name]
        left = con.execute("SELECT COUNT(*) FROM sys.schemas WHERE name = ?", schema).fetchone()
        assert left[0] == 0
    finally:
        with contextlib.suppress(Exception):
            con.execute(f"DROP TABLE IF EXISTS [{schema}].[tick_f0]")
            con.execute(f"DROP SCHEMA IF EXISTS [{schema}]")
        con.close()
        with contextlib.suppress(Exception):
            service.delete_container(name)
