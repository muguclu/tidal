"""Tiering on SQL Server 2022 (real ``sp_getapplock``, DELETE, ``sp_spaceused``) and Azurite
(create-only blobs, blob index tags, duckdb ``azure`` reads). Needs docker (marked ``integration``).

``TIDAL_TEST_SQL_URL`` (``host,port``; password in ``TIDAL_TEST_SQL_PASSWORD`` or
``TIDAL_SQL__PASSWORD``) reuses a running SQL Server with a throwaway database; otherwise a
container is started. Azurite always runs as a throwaway container: the compose one holds the
developer's synthetic data under the same ``marketdata/...`` paths; tests must never write it.
"""

# ruff: noqa: PLC0415, PT018

import asyncio
import os
import re
import tempfile
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

from integration.images import AZURITE_IMAGE, SQL_SERVER_IMAGE
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import Interval
from tidal.query import duck
from tidal.query.reader import UnifiedReader
from tidal.storage import layout, tiering
from tidal.storage import sql_repo as sr
from tidal.storage.compact import compact
from tidal.storage.parquet_writer import AzureBlobStore
from tidal.storage.retag import AzureTagger, retag
from tidal.storage.tiering import run_tiering
from unit.query import query_support as qs
from unit.storage.tiering_support import (
    CFG,
    NOW,
    T0,
    TARGET,
    Boom,
    build_small_baseline,
    cold_rows,
    sql_rows,
)

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[3] / "sql" / "migrations"
PASSWORD = "Tidal_t3st_Passw0rd!"  # noqa: S105 - throwaway container credential
AZURITE_KEY = (  # public Azurite dev key
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


def _settings() -> Settings:
    hidden = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("TIDAL_TEST_")}
    try:
        return Settings.from_env({"TIDAL_ENV": "local"})
    finally:
        os.environ.update(hidden)


SETTINGS = _settings()


def _odbc_driver() -> str:
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
    while True:
        try:
            with eng.connect():
                return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


@pytest.fixture(scope="module")
def server() -> Iterator[tuple[str, str]]:
    shared = os.environ.get("TIDAL_TEST_SQL_URL")
    if shared:
        password = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get(
            "TIDAL_SQL__PASSWORD"
        )
        if not password:
            pytest.skip("TIDAL_TEST_SQL_URL set but no TIDAL_TEST_SQL_PASSWORD")
        yield shared, password
        return
    container = SqlServerContainer(SQL_SERVER_IMAGE, password=PASSWORD)
    container.waiting_for(
        LogMessageWaitStrategy("ready for client connections").with_startup_timeout(480)
    )
    with container as c:
        yield f"{c.get_container_host_ip()},{c.get_exposed_port(1433)}", PASSWORD


@pytest.fixture
def db(server: tuple[str, str]) -> Iterator[Engine]:
    """A fresh throwaway database per test (the tests mutate and delete)."""
    host, password = server
    master = _engine(host, password, "master")
    _wait_for_login(master, 90)
    name = f"tidal_t_{uuid.uuid4().hex[:10]}"
    with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE [{name}]"))
    eng = _engine(host, password, name)
    try:
        yield eng
    finally:
        eng.dispose()
        with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"))
            conn.execute(text(f"DROP DATABASE [{name}]"))
        master.dispose()


@pytest.fixture(scope="module")
def azurite() -> Iterator[tuple[str, str]]:
    """``(connection string, account url)`` of a throwaway Azurite."""
    with (
        DockerContainer(AZURITE_IMAGE)
        .with_exposed_ports(10000)
        .with_command("azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck")
        .waiting_for(LogMessageWaitStrategy("successfully listens").with_startup_timeout(120)) as c
    ):
        host, port = c.get_container_host_ip(), c.get_exposed_port(10000)
        url = f"http://{host}:{port}/devstoreaccount1"
        yield (
            f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={AZURITE_KEY};"
            f"BlobEndpoint={url};",
            url,
        )


@pytest.fixture(scope="module")
def baseline() -> sr.SqlRepository:
    return build_small_baseline()


class Stack:
    """SQL Server holding the whole baseline, plus an empty Azurite ``marketdata`` container."""

    def __init__(
        self, engine: Engine, azurite: tuple[str, str], baseline: sr.SqlRepository
    ) -> None:
        from azure.storage.blob import BlobServiceClient

        self.cs, self.url = azurite
        self.baseline = baseline
        sr.apply_migrations(engine, MIGRATIONS)
        self.clock = FixedClock(NOW)
        self.repo = sr.SqlRepository(engine, self.clock)
        self.repo.upsert_instruments([qs.instrument_row()])
        qs.copy_rows(baseline, engine)
        self.service = BlobServiceClient.from_connection_string(self.cs)
        container = self.service.get_container_client(layout.CONTAINER_MARKETDATA)
        if container.exists():
            for blob in list(container.list_blobs()):
                container.delete_blob(blob.name)
        else:
            container.create_container()
        self.store = AzureBlobStore(self.service)
        self.tagger = AzureTagger(self.service)

    @property
    def engine(self) -> Engine:
        return self.repo.engine

    def migrate(
        self, tables: tuple[str, ...] = ("bar_hot", "tick_hot")
    ) -> list[tiering.TableResult]:
        return run_tiering(self.engine, self.store, self.tagger, self.clock, CFG, tables)


@pytest.fixture
def stack(db: Engine, azurite: tuple[str, str], baseline: sr.SqlRepository) -> Stack:
    return Stack(db, azurite, baseline)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_migration_on_sql_server_and_azurite_keeps_every_row_and_tags_blobs(stack: Stack) -> None:
    before = sql_rows(stack.repo)
    results = stack.migrate()
    assert all(not r.errors and not r.locked for r in results)
    remaining, cold = sql_rows(stack.repo), cold_rows(stack.store)
    assert remaining and all(r[1] >= TARGET for r in remaining)
    assert sorted(remaining + cold) == before
    assert stack.repo.get_watermark("md.bar_hot") == TARGET
    bars = next(r for r in results if r.table == "md.bar_hot")
    assert bars.sql_kb_before is not None and bars.sql_kb_after is not None
    assert bars.bytes_written > 0
    paths = [p for p in stack.store.list("marketdata/v1/") if p.endswith(".parquet")]
    assert paths
    for path in paths:
        part = layout.parse_md_path(path)
        want = {"age_band": "hot", "compacted": "false"} if part.dataset == "ticks" else {}
        assert stack.tagger.get_tags(path) == want  # ADR-0015: ticks only, two tags
    # find-by-tag is how lifecycle rules select blobs
    hits = list(stack.service.find_blobs_by_tags("\"age_band\" = 'hot'"))
    assert len(hits) == sum("/ticks/" in p for p in paths) > 0


def test_reader_over_sql_server_and_azurite_equals_baseline_after_migration(stack: Stack) -> None:
    empty = Path(tempfile.mkdtemp(prefix="tidal-empty-"))
    base_reader = UnifiedReader(
        stack.baseline, duck.local_session(empty), FixedClock(NOW), SETTINGS
    )
    reader = UnifiedReader(
        stack.repo,
        duck.azure_session(
            account_url=stack.url, managed_identity=False, connection_string=stack.cs
        ),
        FixedClock(NOW),
        SETTINGS,
    )
    stack.migrate()
    hi = T0 + 2_000 * qs.DAY
    for interval in (Interval.D1, Interval.H1):
        for as_of in (None, qs.LATE_INGEST):
            want = _run(base_reader.read_bars(qs.WTI, interval, T0, hi, as_of_ns=as_of))
            got = _run(reader.read_bars(qs.WTI, interval, T0, hi, as_of_ns=as_of))
            assert got.rows == want.rows
    want_t = _run(base_reader.read_ticks(qs.WTI, qs.TICK0, qs.TICK0 + 60 * qs.DAY))
    got_t = _run(reader.read_ticks(qs.WTI, qs.TICK0, qs.TICK0 + 60 * qs.DAY))
    assert got_t.rows == want_t.rows and len(want_t.rows) > 100


def test_applock_excludes_a_second_run(stack: Stack) -> None:
    with stack.engine.connect() as holder:
        rc = holder.exec_driver_sql(
            "DECLARE @rc INT; EXEC @rc = sp_getapplock @Resource = 'tiering:md.bar_hot', "
            "@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; SELECT @rc"
        ).scalar()
        assert rc is not None and int(rc) >= 0
        before = sql_rows(stack.repo)
        results = {r.table: r for r in stack.migrate()}
        assert results["md.bar_hot"].locked and not results["md.tick_hot"].locked
        assert [r for r in sql_rows(stack.repo) if r[0].startswith("bars")] == [
            r for r in before if r[0].startswith("bars")
        ]
        holder.exec_driver_sql(
            "EXEC sp_releaseapplock @Resource = 'tiering:md.bar_hot', @LockOwner = 'Session'"
        )
        holder.commit()
    again = {r.table: r for r in stack.migrate()}
    assert not again["md.bar_hot"].locked and again["md.bar_hot"].rows_moved > 0
    assert not any(r.locked for r in stack.migrate())  # the job released its own lock


def test_crash_after_delete_then_rerun_converges_on_sql_server(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = sql_rows(stack.repo)
    real = tiering.delete_chunk

    def crash(*a: Any, **k: Any) -> int:
        real(*a, **k)
        raise Boom

    monkeypatch.setattr(tiering, "delete_chunk", crash)
    with pytest.raises(Boom):
        stack.migrate()
    monkeypatch.undo()
    results = stack.migrate()  # the lock was released by the context manager's finally clause
    assert not any(r.locked or r.errors for r in results)
    assert sorted(sql_rows(stack.repo) + cold_rows(stack.store)) == before
    assert stack.repo.get_watermark("md.bar_hot") == TARGET


def test_compaction_and_retag_on_azurite(stack: Stack) -> None:
    stack.migrate()
    assert len(tiering.part_paths(stack.store, "bars_1d", qs.WTI, 2019, 0)) == 3
    before = sorted(set(cold_rows(stack.store)))
    res = compact(stack.engine, stack.store, stack.tagger, stack.clock, "bars_1d")
    assert not res.errors and res.merged >= 1
    (only,) = tiering.part_paths(stack.store, "bars_1d", qs.WTI, 2019, 0)
    assert stack.tagger.get_tags(only) == {}  # bars are untagged (ADR-0015)
    assert sorted(set(cold_rows(stack.store))) == before
    # Fixture assumption: the small baseline holds ticks only in 2020 starting 2020-01-30
    # (qs.TICK0), so partition 2020-01 exists; every tick part is 2020 data.
    ticks = tiering.part_paths(stack.store, "ticks", qs.WTI, 2020, 1)
    assert ticks
    res = compact(stack.engine, stack.store, stack.tagger, stack.clock, "ticks")
    assert not res.errors
    (tick,) = tiering.part_paths(stack.store, "ticks", qs.WTI, 2020, 1)
    assert stack.tagger.get_tags(tick) == {"age_band": "hot", "compacted": "true"}
    # blobs tagged by the earlier five-tag design are migrated by retag
    stack.tagger.set_tags(
        tick,
        {"dataset": "ticks", "instrument": qs.WTI, "data_year": "2020",
         "age_band": "hot", "compacted": "true"},
    )  # fmt: skip
    stack.tagger.set_tags(only, {"dataset": "bars_1d", "age_band": "hot", "compacted": "true"})
    later = FixedClock(NOW + 4 * 366 * 86_400 * 10**9)
    moved = retag(stack.store, stack.tagger, later, stack.engine)
    all_ticks = [p for p in stack.store.list("marketdata/v1/ticks/") if p.endswith(".parquet")]
    assert all(layout.parse_md_path(p).year == 2020 for p in all_ticks)
    # every tick part ages hot -> cool (2020 vs clock year 2024); the legacy 2020-01 tick part also
    # drops its retired tags (same single write); the legacy bars part is cleared; other bars
    # parts are untagged and unchanged.
    assert moved.changed == len(all_ticks) + 1
    assert stack.tagger.get_tags(only) == {}
    assert stack.tagger.get_tags(tick) == {"age_band": "cool", "compacted": "true"}
    assert retag(stack.store, stack.tagger, later, stack.engine).changed == 0
    again = compact(stack.engine, stack.store, stack.tagger, stack.clock, "ticks")
    assert again.skipped == again.partitions > 0  # single compacted parts are not read


def test_delete_chunk_mismatch_rolls_back_on_sql_server(stack: Stack) -> None:
    n = len(sql_rows(stack.repo))
    with pytest.raises(tiering.TieringDeleteMismatch):  # several versions live in the range
        tiering.delete_chunk(
            stack.engine, tiering.BARS, tiering.DeleteChunk(1, 4, T0, T0 + 5 * qs.DAY, 1)
        )
    assert len(sql_rows(stack.repo)) == n


def test_lock_check_sees_the_session_lock_and_loses_it_on_release(stack: Stack) -> None:
    with tiering.applock(stack.engine, "tiering:md.bar_hot") as lock:
        assert lock
        lock.check()  # APPLOCK_MODE on the owning session says Exclusive
        assert lock.conn is not None
        lock.conn.exec_driver_sql(
            "EXEC sp_releaseapplock @Resource = 'tiering:md.bar_hot', @LockOwner = 'Session'"
        )
        lock.conn.commit()
        with pytest.raises(tiering.LockLost):
            lock.check()


def test_dry_run_and_restatement_after_migration_on_sql_server(stack: Stack) -> None:
    import numpy as np

    before = sql_rows(stack.repo)
    dry = run_tiering(
        stack.engine,
        stack.store,
        stack.tagger,
        stack.clock,
        tiering.TieringConfig(90, dry_run=True),
    )
    assert sql_rows(stack.repo) == before and sum(r.rows_moved for r in dry) > 0
    stack.migrate()
    ts = T0 + 10 * qs.DAY
    top = max(r[3] for r in cold_rows(stack.store) if r[0] == "bars_1d" and r[1] == ts)
    # the repository files a restatement of a migrated key as version 1; tiering re-versions it
    stack.repo.upsert_bars(
        qs.bars_table(np.array([ts]), np.array([7.0]), np.array([1.0]), qs.LATE_INGEST + 5)
    )
    res = stack.migrate()
    assert not any(r.errors for r in res)
    new = max(
        (r for r in cold_rows(stack.store) if r[0] == "bars_1d" and r[1] == ts), key=lambda r: r[3]
    )
    assert new[3] == top + 1 and new[4] == 7.0
    assert not any(r[1] < TARGET for r in sql_rows(stack.repo))
