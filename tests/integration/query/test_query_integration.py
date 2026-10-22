"""Unified reader on SQL Server 2022 + Azurite through the duckdb ``azure`` extension (docker).

The reader's SQL database holds only rows from W_MIN on, a second database holds every row (the
pure-SQL baseline) and the Azurite container holds the cold Parquet tree.
"""

# ruff: noqa: PLC0415

import asyncio
import re
import sys
import time
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pyodbc
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from testcontainers.community.mssql import SqlServerContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "unit" / "query"))
import query_support as qs

from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.models import Tier
from tidal.contracts.schemas import Interval
from tidal.query import duck
from tidal.query.reader import UnifiedReader, tier_for
from tidal.storage import layout
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[3] / "sql" / "migrations"
PASSWORD = "Tidal_t3st_Passw0rd!"  # noqa: S105 - throwaway container credential
AZURITE_KEY = (  # public Azurite dev key
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
SETTINGS = Settings.from_env({"TIDAL_ENV": "local"})


def _odbc_driver() -> str:
    """Newest installed SQL Server ODBC driver (18 in CI/images, 17 on some dev boxes)."""
    found = sorted(
        (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(d.split()[2]),
    )
    if not found:
        pytest.skip("no 'ODBC Driver N for SQL Server' installed")
    return str(found[-1])


def _engine(host: str, port: str, database: str) -> Engine:
    odbc = (
        f"DRIVER={{{_odbc_driver()}}};SERVER={host},{port};DATABASE={database};UID=sa;"
        f"PWD={{{PASSWORD}}};TrustServerCertificate=yes"
    )
    return create_engine(URL.create("mssql+pyodbc", query={"odbc_connect": odbc}))


@pytest.fixture(scope="module")
def engines() -> Iterator[tuple[Engine, Engine]]:
    """(hot, baseline) engines on two databases of one SQL Server container."""
    container = SqlServerContainer("mcr.microsoft.com/mssql/server:2022-latest", password=PASSWORD)
    # SQL Server needs well over the default 120 s on a busy dev box: wait on its own log line.
    container.waiting_for(
        LogMessageWaitStrategy("ready for client connections").with_startup_timeout(480)
    )
    with container as c:
        host, port = c.get_container_host_ip(), str(c.get_exposed_port(1433))
        master = _engine(host, port, "master")
        deadline = time.monotonic() + 90
        while True:  # the server accepts TCP before it accepts logins
            try:
                with master.connect():
                    break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)
        with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("CREATE DATABASE hot"))
            conn.execute(text("CREATE DATABASE base"))
        yield _engine(host, port, "hot"), _engine(host, port, "base")


@pytest.fixture(scope="module")
def azurite_cs() -> Iterator[str]:
    with (
        DockerContainer("mcr.microsoft.com/azure-storage/azurite:3.37.0")
        .with_exposed_ports(10000)
        .with_command("azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck")
        .waiting_for(LogMessageWaitStrategy("successfully listens").with_startup_timeout(120)) as c
    ):
        host, port = c.get_container_host_ip(), c.get_exposed_port(10000)
        yield (
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey={AZURITE_KEY};BlobEndpoint=http://{host}:{port}/devstoreaccount1;"
        )


class Stack:
    def __init__(self, engines: tuple[Engine, Engine], azurite_cs: str) -> None:
        from azure.storage.blob import BlobServiceClient

        hot, base = engines
        for eng in engines:
            sr.apply_migrations(eng, MIGRATIONS)
            sr.SqlRepository(eng).upsert_instruments([qs.instrument_row()])
        source = qs.build_baseline()
        qs.load_hot(source, hot)
        qs.load_hot(source, base, everything=True)
        service = BlobServiceClient.from_connection_string(azurite_cs)
        service.create_container(layout.CONTAINER_MARKETDATA)
        qs.write_cold(source, pw.AzureBlobStore(service))
        self.hot, self.base = sr.SqlRepository(hot), sr.SqlRepository(base)
        self.reader = UnifiedReader(
            self.hot,
            duck.azure_session(
                account_url="http://127.0.0.1:10000/devstoreaccount1",
                managed_identity=False,
                connection_string=azurite_cs,
            ),
            FixedClock(0),
            SETTINGS,
        )


@pytest.fixture(scope="module")
def stack(engines: tuple[Engine, Engine], azurite_cs: str) -> Stack:
    return Stack(engines, azurite_cs)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


WM_BAR = qs.T0 + 500 * qs.DAY
WM_TICK = qs.TICK0 + 4 * qs.DAY
ROUND = 10_000 * qs.DAY
RANGES = [  # (first day, last day, as_of) spanning the watermark in different ways
    (0, 1000, None),
    (450, 560, None),
    (499, 501, None),
    (100, 480, None),
    (520, 900, None),
    (200, 800, qs.INGEST_BASE + qs.DAY),
    (200, 800, qs.INGEST_BASE + ROUND + qs.DAY),
]


@pytest.mark.parametrize(("d0", "d1", "as_of"), RANGES)
def test_bars_equal_sql_baseline_across_the_watermark(
    stack: Stack, d0: int, d1: int, as_of: int | None
) -> None:
    stack.hot.set_watermark("md.bar_hot", WM_BAR)
    lo, hi = qs.T0 + d0 * qs.DAY, qs.T0 + d1 * qs.DAY
    want = stack.base.read_bars(qs.WTI, Interval.D1, lo, hi, as_of_ns=as_of)
    got = _run(stack.reader.read_bars(qs.WTI, Interval.D1, lo, hi, as_of_ns=as_of))
    assert got.tier is tier_for(lo, hi, WM_BAR)
    assert [(r.ts_ns, r.version, r.close) for r in got.rows] == list(
        zip(
            want["ts_ns"].to_pylist(),
            want["version"].to_pylist(),
            want["close"].to_pylist(),
            strict=True,
        )
    )


def test_ticks_equal_sql_baseline_and_stream_matches(stack: Stack) -> None:
    stack.hot.set_watermark("md.tick_hot", WM_TICK)
    lo, hi = qs.TICK0 + 3 * qs.DAY, qs.TICK0 + 6 * qs.DAY
    want = stack.base.read_ticks(qs.WTI, lo, hi)
    got = _run(stack.reader.read_ticks(qs.WTI, lo, hi))
    assert got.tier is Tier.MIXED
    assert [r.ts_ns for r in got.rows] == want["ts_ns"].to_pylist()

    async def streamed() -> list[Any]:
        s = await stack.reader.stream_ticks(qs.WTI, lo, hi)
        assert s.tier is Tier.MIXED
        return [t for b in [x async for x in s.batches] for t in b.column("ts_ns").to_pylist()]

    assert _run(streamed()) == want["ts_ns"].to_pylist()


def test_negative_price_survives_cold_read_and_ping(stack: Stack) -> None:
    stack.hot.set_watermark("md.bar_hot", WM_BAR)
    cold = _run(stack.reader.read_bars(qs.WTI, Interval.D1, qs.T0, WM_BAR))
    assert cold.tier is Tier.COLD
    assert min(b.close for b in cold.rows) < 0
    _run(stack.reader.ping())
