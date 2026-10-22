"""Unified reader on SQL Server 2022 + Azurite through the duckdb ``azure`` extension (docker).

The reader's SQL database holds only rows from W_MIN on, a second database holds every row (the
pure-SQL baseline) and the Azurite container holds the cold Parquet tree.
"""

# ruff: noqa: PLC0415

import asyncio
import os
import re
import time
import uuid
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

from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.models import Tier
from tidal.contracts.schemas import Interval
from tidal.query import duck
from tidal.query.reader import UnifiedReader, tier_for
from tidal.storage import layout
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr
from unit.query import query_support as qs

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[3] / "sql" / "migrations"
PASSWORD = "Tidal_t3st_Passw0rd!"  # noqa: S105 - throwaway container credential
AZURITE_KEY = (  # public Azurite dev key
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


def _settings() -> Settings:
    """Settings reject unknown TIDAL_* keys in the process environment; hide the test-only ones."""
    hidden = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("TIDAL_TEST_")}
    try:
        return Settings.from_env({"TIDAL_ENV": "local"})
    finally:
        os.environ.update(hidden)


SETTINGS = _settings()


def _odbc_driver() -> str:
    """Newest installed SQL Server ODBC driver (18 in CI/images, 17 on some dev boxes)."""
    found = sorted(
        (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(d.split()[2]),
    )
    if not found:
        pytest.skip("no 'ODBC Driver N for SQL Server' installed")
    return str(found[-1])


def _engine(server: str, password: str, database: str) -> Engine:
    odbc = (
        f"DRIVER={{{_odbc_driver()}}};SERVER={server};DATABASE={database};UID=sa;"
        f"PWD={{{password}}};TrustServerCertificate=yes"
    )
    return create_engine(URL.create("mssql+pyodbc", query={"odbc_connect": odbc}))


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
def engines() -> Iterator[tuple[Engine, Engine]]:
    """(hot, baseline) engines on two throwaway databases.

    ``TIDAL_TEST_SQL_URL`` (``host,port``, password in ``TIDAL_TEST_SQL_PASSWORD`` or
    ``TIDAL_SQL__PASSWORD``) reuses a running SQL Server; otherwise a testcontainer is started.
    """
    shared = os.environ.get("TIDAL_TEST_SQL_URL")
    if shared:
        password = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get(
            "TIDAL_SQL__PASSWORD"
        )
        if not password:
            pytest.skip("TIDAL_TEST_SQL_URL set but no TIDAL_TEST_SQL_PASSWORD")
        yield from _two_databases(shared, password)
        return
    container = SqlServerContainer("mcr.microsoft.com/mssql/server:2022-latest", password=PASSWORD)
    # SQL Server needs well over the default 120 s on a busy dev box: wait on its own log line.
    container.waiting_for(
        LogMessageWaitStrategy("ready for client connections").with_startup_timeout(480)
    )
    with container as c:
        server = f"{c.get_container_host_ip()},{c.get_exposed_port(1433)}"
        yield from _two_databases(server, PASSWORD)


def _two_databases(server: str, password: str) -> Iterator[tuple[Engine, Engine]]:
    master = _engine(server, password, "master")
    _wait_for_login(master, 90)
    suffix = uuid.uuid4().hex[:10]
    names = (f"tidal_q_hot_{suffix}", f"tidal_q_base_{suffix}")
    with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for name in names:
            conn.execute(text(f"CREATE DATABASE [{name}]"))
    engines = _engine(server, password, names[0]), _engine(server, password, names[1])
    try:
        yield engines
    finally:
        for eng in engines:
            eng.dispose()
        with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for name in names:
                conn.execute(
                    text(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                )
                conn.execute(text(f"DROP DATABASE [{name}]"))
        master.dispose()


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
        service = BlobServiceClient.from_connection_string(azurite_cs)
        service.create_container(layout.CONTAINER_MARKETDATA)
        baseline, hot_src = qs.make_world(pw.AzureBlobStore(service), mid_migration=False)
        qs.copy_rows(hot_src, hot)  # hot rows plus late restatements of cold keys
        qs.copy_rows(baseline, base)
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
    # SQL still holds late restatements below the watermark, which makes such ranges mixed.
    hot_ts = stack.hot.read_bars(qs.WTI, Interval.D1, lo, hi)["ts_ns"].to_numpy()
    hot_below = bool((hot_ts < WM_BAR).any())
    cold_above = hi > WM_BAR and lo < qs.BAR_WM_RANGE[1]  # Parquet also holds days up to W_MAX
    assert got.tier is (Tier.MIXED if hot_below or cold_above else tier_for(lo, hi, WM_BAR))
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
    assert cold.tier in (Tier.COLD, Tier.MIXED)  # mixed: late restatements remain in SQL
    assert min(b.close for b in cold.rows) < 0
    _run(stack.reader.ping())
