"""Synth S, one instrument-month, into Azurite + SQL Server containers (needs docker)."""

# ruff: noqa: S105

import re
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pyodbc
import pytest
from azure.storage.blob import BlobServiceClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from testcontainers.community.mssql import SqlServerContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from integration.images import AZURITE_IMAGE, SQL_SERVER_IMAGE
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock, to_ts_ns
from tidal.contracts.schemas import Interval
from tidal.ingest import cli
from tidal.ingest.router import read_part
from tidal.ingest.sources import synthetic as syn
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import AzureBlobStore

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[3] / "sql" / "migrations"
PASSWORD = "Tidal_t3st_Passw0rd!"
NOW = "2025-01-31T23:59:59+00:00"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


def _odbc_driver() -> str:
    found = sorted(
        (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(d.split()[2]),
    )
    if not found:
        pytest.skip("no 'ODBC Driver N for SQL Server' installed")
    return str(found[-1])


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with SqlServerContainer(SQL_SERVER_IMAGE, password=PASSWORD) as c:
        odbc = (
            f"DRIVER={{{_odbc_driver()}}};SERVER={c.get_container_host_ip()},"
            f"{c.get_exposed_port(1433)};DATABASE=master;UID=sa;PWD={{{PASSWORD}}};"
            "TrustServerCertificate=yes"
        )
        eng = create_engine(
            URL.create("mssql+pyodbc", query={"odbc_connect": odbc}), fast_executemany=True
        )
        deadline = time.monotonic() + 90
        while True:
            try:
                with eng.connect():
                    break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)
        sr.apply_migrations(eng, MIGRATIONS)
        yield eng


@pytest.fixture(scope="module")
def service() -> Iterator[BlobServiceClient]:
    with (
        DockerContainer(AZURITE_IMAGE)
        .with_exposed_ports(10000)
        .with_command("azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck")
        .waiting_for(LogMessageWaitStrategy("successfully listens").with_startup_timeout(120)) as c
    ):
        host, port = c.get_container_host_ip(), c.get_exposed_port(10000)
        svc = BlobServiceClient.from_connection_string(
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey={KEY};BlobEndpoint=http://{host}:{port}/devstoreaccount1;"
        )
        svc.create_container(layout.CONTAINER_MARKETDATA)
        yield svc


@pytest.mark.usefixtures("hidden_test_env")
def test_synth_s_one_instrument_month_hot_and_cold(
    engine: Engine, service: BlobServiceClient
) -> None:
    settings = Settings.from_env(
        {
            "TIDAL_ENV": "local",
            "TIDAL_HOT_WINDOW_DAYS": "10",
            "TIDAL_CLOCK__NOW": NOW,
            "TIDAL_SYNTH__START": "2025-01-01",
            "TIDAL_SYNTH__END": "2025-01-31",
        }
    )
    clock = FixedClock(to_ts_ns(settings.clock.now))  # type: ignore[arg-type]
    store = AzureBlobStore(service)
    ctx = cli.Context(settings, store, sr.SqlRepository(engine, clock), clock)
    units = syn.plan("S", date(2025, 1, 1), date(2025, 1, 31))[:1]
    iid = units[0].instrument_id

    routed = cli.run_synth(ctx, "S", 1, 1, units).routed

    cutoff = clock.now_ns() - 10 * 86_400 * 10**9
    assert routed.hot > 0
    assert routed.cold > 0
    tick_part = f"marketdata/v1/ticks/instrument={iid}/year=2025/month=01/part-00000.parquet"
    cold = read_part(store, tick_part)
    assert cold.num_rows > 0
    assert cold.column("ts_ns").to_numpy().max() < cutoff
    hot = ctx.repo.read_ticks(iid, 0, to_ts_ns(settings.clock.now) + 1)  # type: ignore[arg-type]
    assert hot.column("ts_ns").to_numpy().min() >= cutoff
    for interval in ("1s", "1m", "1h", "1d"):
        dataset = f"bars_{interval}"
        month = 0 if interval == "1d" else 1
        assert list(store.list(layout.md_partition_prefix(dataset, iid, 2025, month)))
    assert ctx.repo.read_bars(iid, Interval.H1, 0, clock.now_ns() + 1).num_rows > 0
    with engine.connect() as c:
        row = c.execute(
            text("SELECT first_ts_ns, last_ts_ns FROM ref.instrument WHERE instrument_id = :i"),
            {"i": iid},
        ).one()
    assert row.first_ts_ns is not None
    assert row.last_ts_ns >= cutoff
