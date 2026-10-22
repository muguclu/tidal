"""tidal-api -> tidal-query -> unified reader on SQL Server + Azurite, with daily-history data.

Needs the compose stack: ``TIDAL_TEST_SQL_URL`` (``host,port``) and ``TIDAL_TEST_SQL_PASSWORD``
(or ``TIDAL_SQL__PASSWORD``) for SQL Server, and Azurite on 127.0.0.1:10000 (dev key). The SQL
database is throwaway; Azurite is shared, so assertions compare against the in-process reader and
the ingest result instead of absolute row counts.
"""

# ruff: noqa: PLC0415

import os
import re
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pyodbc
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from tidal.api.app import create_app
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock, to_ts_ns
from tidal.contracts.models import Tier
from tidal.contracts.schemas import Interval
from tidal.ingest import cli
from tidal.query import duck
from tidal.query.reader import UnifiedReader
from tidal.query.service import create_query_app, remote_reader_from_settings
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import AzureBlobStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MIGRATIONS = Path(__file__).resolve().parents[3] / "sql" / "migrations"
NOW = "2025-01-31T00:00:00+00:00"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
AZURITE_URL = "http://127.0.0.1:10000/devstoreaccount1"
AZURITE_CS = (
    f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={KEY};"
    f"BlobEndpoint={AZURITE_URL};"
)
IID = "EIA.WTI.SPOT"
DAY = 86_400 * 10**9
WINDOW_DAYS = 30


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _engine(server: str, password: str, database: str) -> Engine:
    drivers = sorted(
        d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)
    )
    if not drivers:
        pytest.skip("no ODBC Driver for SQL Server")
    odbc = (
        f"DRIVER={{{drivers[-1]}}};SERVER={server};DATABASE={database};UID=sa;"
        f"PWD={{{password}}};TrustServerCertificate=yes"
    )
    return create_engine(URL.create("mssql+pyodbc", query={"odbc_connect": odbc}))


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    server = os.environ.get("TIDAL_TEST_SQL_URL")
    password = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get("TIDAL_SQL__PASSWORD")
    if not server or not password:
        pytest.skip("TIDAL_TEST_SQL_URL / TIDAL_TEST_SQL_PASSWORD not set")
    master = _engine(server, password, "master")
    name = f"tidal_qs_{uuid.uuid4().hex[:10]}"
    with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE [{name}]"))
    eng = _engine(server, password, name)
    sr.apply_migrations(eng, MIGRATIONS)
    try:
        yield eng
    finally:
        eng.dispose()
        with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"))
            conn.execute(text(f"DROP DATABASE [{name}]"))
        master.dispose()


def _settings() -> Settings:
    hidden = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("TIDAL_TEST_")}
    try:
        return Settings.from_env(
            {
                "TIDAL_ENV": "local",
                "TIDAL_AUTH__DISABLED": "true",
                "TIDAL_HOT_WINDOW_DAYS": str(WINDOW_DAYS),
                "TIDAL_CLOCK__NOW": NOW,
            }
        )
    finally:
        os.environ.update(hidden)


@pytest.fixture(scope="module")
def world(engine: Engine) -> tuple[UnifiedReader, int]:
    """Daily history split hot/cold by the router; watermark = the hot-window cutoff."""
    from azure.storage.blob import BlobServiceClient

    try:
        service = BlobServiceClient.from_connection_string(AZURITE_CS)
        if not service.get_container_client(layout.CONTAINER_MARKETDATA).exists():
            service.create_container(layout.CONTAINER_MARKETDATA)
    except Exception:
        pytest.skip("Azurite is not reachable on 127.0.0.1:10000")
    settings = _settings()
    clock = FixedClock(to_ts_ns(settings.clock.now))  # type: ignore[arg-type]
    repo = sr.SqlRepository(engine, clock)
    ctx = cli.Context(settings, AzureBlobStore(service), repo, clock)
    res = cli.run_daily_history(ctx, seed=7, start=date(2024, 10, 1))
    assert res.routed.hot > 0
    cutoff = clock.now_ns() - WINDOW_DAYS * DAY
    repo.set_watermark("md.bar_hot", cutoff)
    session = duck.azure_session(
        account_url=AZURITE_URL,
        managed_identity=False,
        connection_string=AZURITE_CS,
        allow_install=True,
    )
    return UnifiedReader(repo, session, clock, settings), cutoff


async def test_api_reads_through_tidal_query_match_the_reader(
    world: tuple[UnifiedReader, int],
) -> None:
    reader, cutoff = world
    settings = _settings()
    query = create_query_app(reader, validator=_Never(), settings=settings)  # auth disabled local
    remote = remote_reader_from_settings(settings, transport=httpx.ASGITransport(app=query))
    app = create_app(settings, reader=remote, clock=FixedClock(cutoff + 40 * DAY))
    first = cutoff - 20 * DAY

    def window(lo: int, hi: int) -> dict[str, str]:
        return {
            "instrument_id": IID, "interval": "1d", "start": str(lo), "end": str(hi),
        }  # fmt: skip

    cases = {
        Tier.COLD: window(first, cutoff),
        Tier.HOT: window(cutoff, cutoff + 29 * DAY),
        Tier.MIXED: window(first, cutoff + 10 * DAY),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for tier, q in cases.items():
            want = await reader.read_bars(IID, Interval.D1, int(q["start"]), int(q["end"]))
            r = await c.get("/v1/bars", params={**q, "limit": 1000})
            assert r.status_code == 200, r.text
            assert r.headers["X-Tidal-Tier"] == tier.value == want.tier.value
            assert [row["ts_ns"] for row in r.json()["rows"]] == [b.ts_ns for b in want.rows]
            assert [row["close"] for row in r.json()["rows"]] == [b.close for b in want.rows]
            a = await c.get("/v1/bars", params={**q, "format": "arrow"})
            assert a.headers["X-Tidal-Tier"] == tier.value
        ids = await c.get("/v1/instruments")
        assert IID in {i["instrument_id"] for i in ids.json()["items"]}
        latest = await c.get("/v1/latest", params={"instrument_id": IID})
        assert latest.headers["X-Tidal-Tier"] == "hot"
        assert latest.json()["rows"][0]["instrument_id"] == IID
        miss = await c.get("/v1/bars", params={**window(first, cutoff), "instrument_id": "X.NOPE"})
        assert miss.status_code in (400, 404)


class _Never:
    async def validate(self, token: str) -> None:
        raise AssertionError("auth is disabled in local; the validator must not be called")
