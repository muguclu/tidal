"""Integration tests: SQL Server 2022 and Azurite containers (need docker; marked ``integration``).

NOT executed on the box that authored them (no docker). Run with ``uv run pytest -m integration``.
"""

# ruff: noqa: PLC0415, PT018

import math
import os
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyodbc
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from testcontainers.community.mssql import SqlServerContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import ExecWaitStrategy, LogMessageWaitStrategy

from tidal.contracts.schemas import BAR_V1, TICK_V1, Interval
from tidal.storage import layout
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[3] / "sql" / "migrations"
T0 = 1_587_340_800 * 10**9
DAY = 86_400 * 10**9
PASSWORD = "Tidal_t3st_Passw0rd!"  # noqa: S105 - throwaway container credential


def _odbc_driver() -> str:
    """Newest installed SQL Server ODBC driver (18 in CI/images, 17 on some dev boxes)."""
    found = sorted(
        (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(d.split()[2]),
    )
    if not found:
        pytest.skip("no 'ODBC Driver N for SQL Server' installed")
    return str(found[-1])


class _SlowStartSqlServer(SqlServerContainer):
    """SqlServerContainer with a 300 s readiness timeout (shared Docker hosts are slow)."""

    def _connect(self) -> None:
        ExecWaitStrategy(
            [
                "bash",
                "-c",
                '/opt/mssql-tools*/bin/sqlcmd -U "$SQLSERVER_USER" -P "$SA_PASSWORD" '
                "-Q 'SELECT 1' -C",
            ]
        ).with_startup_timeout(300).wait_until_ready(self)


def _engine_for(odbc_server: str, user: str, password: str, database: str) -> Engine:
    odbc = (
        f"DRIVER={{{_odbc_driver()}}};SERVER={odbc_server};DATABASE={database};"
        f"UID={user};PWD={{{password}}};TrustServerCertificate=yes"
    )
    return create_engine(
        URL.create("mssql+pyodbc", query={"odbc_connect": odbc}), fast_executemany=True
    )


def _wait_for_login(eng: Engine, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:  # the server accepts TCP before it accepts logins
        try:
            with eng.connect():
                return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A SQL Server: ``TIDAL_TEST_SQL_URL`` (``host,port``; SA password from
    ``TIDAL_TEST_SQL_PASSWORD`` or ``TIDAL_SQL__PASSWORD``) with a throwaway database, else a
    testcontainer."""
    shared = os.environ.get("TIDAL_TEST_SQL_URL")
    if shared:
        pw_env = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get("TIDAL_SQL__PASSWORD")
        if not pw_env:
            pytest.skip("TIDAL_TEST_SQL_URL set but no TIDAL_TEST_SQL_PASSWORD")
        admin = _engine_for(shared, "sa", pw_env, "master").execution_options(
            isolation_level="AUTOCOMMIT"
        )
        _wait_for_login(admin, 60)
        db = f"tidal_test_{uuid.uuid4().hex[:12]}"
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE [{db}]"))
        eng = _engine_for(shared, "sa", pw_env, db)
        try:
            yield eng
        finally:
            eng.dispose()
            with admin.connect() as conn:
                conn.execute(text(f"ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"))
                conn.execute(text(f"DROP DATABASE [{db}]"))
            admin.dispose()
        return
    with _SlowStartSqlServer("mcr.microsoft.com/mssql/server:2022-latest", password=PASSWORD) as c:
        eng = _engine_for(
            f"{c.get_container_host_ip()},{c.get_exposed_port(1433)}", "sa", PASSWORD, "master"
        )
        _wait_for_login(eng, 60)
        yield eng


@pytest.fixture(scope="module")
def repo(engine: Engine) -> sr.SqlRepository:
    sr.apply_migrations(engine, MIGRATIONS)
    r = sr.SqlRepository(engine)
    r.upsert_instruments(
        [
            {
                "instrument_id": "EIA.WTI.SPOT", "source": "EIA", "asset_class": "crude",
                "kind": "spot", "unit": "USD/bbl", "currency": "USD",
                "exchange_tz": "America/Chicago", "granularity": "1d", "roll_method": None,
                "first_ts_ns": None, "last_ts_ns": None, "description": "EIA RWTC, spot, USD/bbl",
            }
        ]
    )  # fmt: skip
    return r


def test_migrations_apply_idempotently_and_create_contract_tables(engine: Engine) -> None:
    assert sr.apply_migrations(engine, MIGRATIONS) in ([], ["0001_init"])
    assert sr.apply_migrations(engine, MIGRATIONS) == []
    with engine.connect() as c:
        names = {
            f"{r[0]}.{r[1]}"
            for r in c.execute(
                text("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES")
            )
        }
        compression = {
            r[0]: r[1]
            for r in c.execute(
                text(
                    "SELECT OBJECT_NAME(object_id), data_compression_desc FROM sys.partitions "
                    "WHERE OBJECT_NAME(object_id) IN ('bar_hot','tick_hot','bar_baseline') "
                    "AND index_id = 1"
                )
            )
        }
    assert {"md.bar_hot", "md.tick_hot", "md.bar_baseline", "ops.schema_version"} <= names
    assert compression == {"bar_hot": "PAGE", "tick_hot": "PAGE", "bar_baseline": "NONE"}


def _bars(prices: list[float], ingested: int) -> pa.Table:
    n = len(prices)
    px = np.array(prices)
    return pa.table(
        {
            "ts_ns": T0 + np.arange(n, dtype=np.int64) * DAY,
            "instrument_id": pa.array(["EIA.WTI.SPOT"] * n)
            .dictionary_encode()
            .cast(BAR_V1.field(1).type),
            "interval": pa.array(["1d"] * n).dictionary_encode().cast(BAR_V1.field(2).type),
            "open": px, "high": px, "low": px, "close": px,
            "volume": np.full(n, np.nan),
            "trade_count": pa.array([None] * n, pa.int32()),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, ingested, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=BAR_V1,
    )  # fmt: skip


def test_bars_versioning_nan_null_and_as_of_on_sql_server(repo: sr.SqlRepository) -> None:
    assert repo.upsert_bars(_bars([-37.63, 18.27], T0)) == 2
    assert repo.upsert_bars(_bars([-37.63, 18.27], T0 + 1)) == 0
    assert repo.upsert_bars(_bars([-37.63, 19.0], T0 + 100)) == 1
    rng = (T0, T0 + 5 * DAY)
    latest = repo.read_bars("EIA.WTI.SPOT", Interval.D1, *rng)
    assert latest["close"].to_pylist() == [-37.63, 19.0]
    assert all(math.isnan(v or 0.0) for v in latest["volume"].to_pylist())
    old = repo.read_bars("EIA.WTI.SPOT", Interval.D1, *rng, as_of_ns=T0 + 50)
    assert old["close"].to_pylist() == [-37.63, 18.27]
    with repo.engine.connect() as c:  # NaN is stored as NULL, never as a float
        nulls = c.execute(text("SELECT COUNT(*) FROM md.bar_hot WHERE volume IS NULL")).scalar_one()
    assert nulls == 3


def test_ticks_real_column_and_watermark(repo: sr.SqlRepository) -> None:
    n = 5
    t = pa.table(
        {
            "ts_ns": T0 + np.arange(n, dtype=np.int64),
            "instrument_id": pa.array(["EIA.WTI.SPOT"] * n)
            .dictionary_encode()
            .cast(TICK_V1.field(1).type),
            "price": np.linspace(-1, 1, n),
            "size": np.full(n, 0.5, np.float32),
            "side": np.zeros(n, np.int8),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, T0, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=TICK_V1,
    )
    assert repo.upsert_ticks(t) == n
    assert repo.read_ticks("EIA.WTI.SPOT", T0, T0 + n).equals(t)
    repo.set_watermark("md.tick_hot", 42)
    repo.set_watermark("md.tick_hot", 43)
    assert repo.get_watermark("md.tick_hot") == 43


@pytest.fixture(scope="module")
def azurite_url() -> Iterator[str]:
    with (
        DockerContainer("mcr.microsoft.com/azure-storage/azurite:3.37.0")
        .with_exposed_ports(10000)
        .with_command("azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck")
        .waiting_for(LogMessageWaitStrategy("successfully listens").with_startup_timeout(120)) as c
    ):
        host, port = c.get_container_host_ip(), c.get_exposed_port(10000)
        key = (  # public Azurite dev key
            "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
        )
        yield (
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey={key};BlobEndpoint=http://{host}:{port}/devstoreaccount1;"
        )


def test_writer_against_azurite_blob(azurite_url: str, tmp_path: Path) -> None:
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(azurite_url)
    service.create_container(layout.CONTAINER_MARKETDATA)
    store = pw.AzureBlobStore(service)
    n = 1000
    t = pa.table(
        {
            "ts_ns": 1709251200 * 10**9 + np.arange(n, dtype=np.int64),
            "instrument_id": pa.array(["SYN.BRENT.TICK"] * n)
            .dictionary_encode()
            .cast(TICK_V1.field(1).type),
            "price": np.linspace(70, 80, n),
            "size": np.ones(n, np.float32),
            "side": np.zeros(n, np.int8),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.zeros(n, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=TICK_V1,
    )
    kw = {"dataset": "ticks", "instrument_id": "SYN.BRENT.TICK", "year": 2024, "month": 3}
    [a] = pw.write_partition(store, batches=[t], source_hash="h", **kw)  # type: ignore[arg-type]
    [b] = pw.write_partition(store, batches=[t], source_hash="h", **kw)  # type: ignore[arg-type]
    assert (layout.parse_md_path(a).seq, layout.parse_md_path(b).seq) == (0, 1)
    real_file = tmp_path / "real.parquet"
    real_file.write_bytes(b"PAR1")
    with pytest.raises(pw.ImmutableFileExists):
        store.put_new(a, real_file)  # the blob exists, so create-only must refuse
    _, name = layout.split_container(a)
    data = service.get_blob_client(layout.CONTAINER_MARKETDATA, name).download_blob().readall()
    local = tmp_path / "p.parquet"
    local.write_bytes(data)
    assert pq.read_table(local).num_rows == n


def test_azure_store_is_create_only_and_lists(azurite_url: str, tmp_path: Path) -> None:
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(azurite_url)
    service.create_container("tidalstoretest")
    store = pw.AzureBlobStore(service)
    f = tmp_path / "f.bin"
    f.write_bytes(b"PAR1")
    store.put_new("tidalstoretest/v1/a/part-00000.parquet", f)
    with pytest.raises(pw.ImmutableFileExists):
        store.put_new("tidalstoretest/v1/a/part-00000.parquet", f)
    assert list(store.list("tidalstoretest/v1/a/")) == ["tidalstoretest/v1/a/part-00000.parquet"]
    assert list(store.list("tidalstoretest/v1/b/")) == []


def test_azure_store_read_list_delete(azurite_url: str, tmp_path: Path) -> None:
    from azure.storage.blob import BlobServiceClient

    from tidal.common.errors import NotFound

    service = BlobServiceClient.from_connection_string(azurite_url)
    service.create_container("tidalrwtest")
    store = pw.AzureBlobStore(service)
    f = tmp_path / "f.bin"
    f.write_bytes(b"PAR1xyz")
    paths = ["tidalrwtest/v1/b/part-00001.parquet", "tidalrwtest/v1/a/part-00000.parquet"]
    other = "tidalrwtest/v1x/part-00000.parquet"
    for p in [*paths, other]:
        store.put_new(p, f)
    assert store.read(paths[0]) == b"PAR1xyz"
    assert store.exists(paths[0]) and not store.exists("tidalrwtest/v1/nope")
    with pytest.raises(NotFound):
        store.read("tidalrwtest/v1/nope")
    assert list(store.list("tidalrwtest/v1/")) == sorted(paths)
    assert list(store.list("tidalrwtest")) == list(store.list("tidalrwtest/")) != []
    assert list(store.list("nosuchcontainer")) == []
    with pytest.raises(layout.InvalidPath):
        list(store.list(""))
    assert list(store.list("tidalrwtest/v1")) == sorted([*paths, other])
    store.delete(paths[0])
    store.delete(paths[0])  # idempotent
    assert not store.exists(paths[0]) and store.exists(paths[1]) and store.exists(other)
