"""Crash injection for the tiering migration and compaction (T-206, SPEC-03 §2 and §6).

Real SQL Server (applocks, DELETE) and a throwaway Azurite (create-only blobs, index tags).

* ``test_kill_*``: the job runs in a subprocess that is killed (SIGKILL / TerminateProcess) at one
  step of the documented step order; a re-run must end in the state of an uninterrupted run.
* ``test_killed_session_releases_the_applock``: the SQL session of a killed process loses its lock.
* ``test_property_crash_then_rerun_equals_uninterrupted_run``: hypothesis over data shapes (late
  restatements, A -> B -> A reverts, equal-value duplicates, months rolling past a tiny
  ``MAX_PART_ROWS``) and random crash points; in-process faults (``Boom``) to keep it fast.
* ``test_reader_*``: 200 random ranges, unified reader == pure-SQL baseline, before and after.

"Same final state" is SPEC-03 §6: the same SQL rows, the same cold rows including ``version``,
``ingested_at_ns``, ``quality`` and the value columns, and the same watermarks. File layout is not
compared. Environment: ``TIDAL_TEST_SQL_PASSWORD`` or ``TIDAL_SQL__PASSWORD`` (+ optional
``TIDAL_TEST_SQL_URL``, default ``127.0.0.1,1433``) selects a running SQL Server; without a password
the tests skip unless ``TIDAL_TEST_SQL_CONTAINER=1`` starts a throwaway SQL Server container;
with ``CI`` or ``TIDAL_TEST_REQUIRE_SQL=1`` a missing driver or password fails.
``TIDAL_TEST_AZURITE_CS``
selects an existing Azurite (emptied by the tests) instead of a throwaway container.
``TIDAL_TEST_CRASH_EXAMPLES`` sets the crash property examples (default 4 for PRs, 40 nightly);
``TIDAL_TEST_CRASH_FULL=1`` (nightly) widens the kill matrix to several occurrences of every step.
"""

# ruff: noqa: PLR0917, S603

import asyncio
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import polars as pl
import pyarrow.parquet as pq
import pyodbc
import pytest
from azure.storage.blob import BlobServiceClient
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, create_engine, insert, text
from sqlalchemy.engine import URL
from testcontainers.community.mssql import SqlServerContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from integration.crash_support import (
    COMPACT_POINTS,
    MIGRATE_POINTS,
    Boom,
    Fault,
    Params,
    run_once,
)
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock, month_bounds
from tidal.contracts.models import Tier
from tidal.contracts.schemas import INTERVAL_CODES, Interval, QualityFlag
from tidal.query import duck
from tidal.query.reader import UnifiedReader
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import AzureBlobStore
from tidal.storage.retag import AzureTagger
from tidal.storage.tiering import run_tiering
from unit.query import query_support as qs
from unit.storage.tiering_support import CFG, NOW, T0, TARGET, build_small_baseline

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[2] / "sql" / "migrations"
WORKER = Path(__file__).with_name("crash_support.py")
PASSWORD = "Tidal_t3st_Passw0rd!"  # noqa: S105 - throwaway container credential
AZURITE_KEY = (  # public Azurite dev key
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
KILLED_CODE: Final = 15 if sys.platform == "win32" else -9  # TerminateProcess code
CRASH_EXAMPLES = int(os.environ.get("TIDAL_TEST_CRASH_EXAMPLES", "4"))
DAY, HOUR = qs.DAY, qs.HOUR


def _settings() -> Settings:
    hidden = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("TIDAL_TEST_")}
    try:
        return Settings.from_env({"TIDAL_ENV": "local"})
    finally:
        os.environ.update(hidden)


SETTINGS = _settings()


# -- infrastructure ------------------------------------------------------------------------------
def _require() -> bool:
    """CI (and ``TIDAL_TEST_REQUIRE_SQL=1``) must run these tests: a missing piece is a failure."""
    return bool(os.environ.get("CI")) or os.environ.get("TIDAL_TEST_REQUIRE_SQL") == "1"


def _unavailable(reason: str) -> None:
    """Skip locally, fail where the suite is required to run."""
    if _require():
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def _odbc(server: str, password: str, database: str) -> str:
    drivers = sorted(
        (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(d.split()[2]),
    )
    if not drivers:
        _unavailable("no 'ODBC Driver N for SQL Server' installed")
    return (
        f"DRIVER={{{drivers[-1]}}};SERVER={server};DATABASE={database};UID=sa;"
        f"PWD={{{password}}};TrustServerCertificate=yes"
    )


def _engine(server: str, password: str, database: str) -> Engine:
    return create_engine(
        URL.create("mssql+pyodbc", query={"odbc_connect": _odbc(server, password, database)})
    )


def _poll[T](what: str, probe: Callable[[], T | None], timeout: float, every: float = 0.25) -> T:
    """Call ``probe`` until it returns something other than ``None`` (no blind sleeps)."""
    deadline = time.monotonic() + timeout
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(every)


@pytest.fixture(scope="module")
def server() -> Iterator[tuple[str, str]]:
    """``(host,port, sa password)`` of a running SQL Server, or of a throwaway container."""
    password = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get("TIDAL_SQL__PASSWORD")
    if password:
        yield os.environ.get("TIDAL_TEST_SQL_URL", "127.0.0.1,1433"), password
        return
    if os.environ.get("TIDAL_TEST_SQL_CONTAINER") != "1":
        _unavailable(
            "no SQL Server: set TIDAL_TEST_SQL_PASSWORD (or TIDAL_SQL__PASSWORD) for the "
            "compose server, or TIDAL_TEST_SQL_CONTAINER=1 for a throwaway container"
        )
    container = SqlServerContainer("mcr.microsoft.com/mssql/server:2022-latest", password=PASSWORD)
    container.waiting_for(
        LogMessageWaitStrategy("ready for client connections").with_startup_timeout(480)
    )
    with container as c:
        yield f"{c.get_container_host_ip()},{c.get_exposed_port(1433)}", PASSWORD


@pytest.fixture(scope="module")
def database(server: tuple[str, str]) -> Iterator[tuple[Engine, str]]:
    """A throwaway database for the module: ``(engine, odbc connection string)``."""
    host, password = server

    def master() -> Engine:
        return _engine(host, password, "master")

    def connected() -> bool | None:
        try:
            with master().connect():
                return True
        except Exception:
            return None

    _poll("SQL Server login", connected, 300, 2)
    name = f"tidal_crash_{uuid.uuid4().hex[:10]}"
    with master().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE [{name}]"))
    eng = _engine(host, password, name)
    try:
        yield eng, _odbc(host, password, name)
    finally:
        eng.dispose()
        with master().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"))
            conn.execute(text(f"DROP DATABASE [{name}]"))


@pytest.fixture(scope="module")
def azurite() -> Iterator[tuple[str, str]]:
    """``(connection string, account url)`` of Azurite.

    ``TIDAL_TEST_AZURITE_CS`` (the CI service container) is used as given and emptied by the
    tests; otherwise a throwaway container is started (never the compose one: real data).
    """
    given = os.environ.get("TIDAL_TEST_AZURITE_CS")
    if given:
        endpoint = re.search(r"BlobEndpoint=([^;]+)", given)
        if endpoint is None:
            pytest.fail("TIDAL_TEST_AZURITE_CS has no BlobEndpoint=...", pytrace=False)
        yield given, endpoint.group(1)
        return
    with (
        DockerContainer("mcr.microsoft.com/azure-storage/azurite:3.37.0")
        .with_exposed_ports(10000)
        .with_command("azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck")
        .waiting_for(LogMessageWaitStrategy("successfully listens").with_startup_timeout(120)) as c
    ):
        url = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(10000)}/devstoreaccount1"
        yield (
            f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={AZURITE_KEY};"
            f"BlobEndpoint={url};",
            url,
        )


# -- the world: a SQL Server database + an Azurite container --------------------------------------
Row = tuple[Any, ...]
Snapshot = tuple[list[Row], list[Row], int | None, int | None]  # sql, cold, bar wm, tick wm


def _norm(value: float | None) -> float | None:
    return None if value is None or math.isnan(value) else float(value)  # NULL and NaN are one


def finish(proc: subprocess.Popen[str], timeout: float = 600) -> tuple[str, str]:
    """``communicate`` with a deadline; a hung child is killed and reaped before the test fails."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        pytest.fail(f"worker still running after {timeout:.0f}s; killed\n{err[-2000:]}")


def _row(dataset: str, iid: str, interval: str | None, r: dict[str, Any], ticks: bool) -> Row:
    """Whole row: key, version, quality, ingest time, identity and every value column."""
    names = (
        ("price", "size") if ticks else ("open", "high", "low", "close", "volume", "trade_count")
    )
    values = tuple(r[n] if n == "trade_count" else _norm(r[n]) for n in names)
    return (
        dataset, r["ts_ns"], r["side"] if ticks else 0, r["version"], r["quality"],
        r["ingested_at_ns"], iid, interval, values,
    )  # fmt: skip


class World:
    """SQL Server holding the hot tables and an Azurite ``marketdata`` container."""

    def __init__(self, database: tuple[Engine, str], azurite: tuple[str, str]) -> None:
        self.engine, self.odbc = database
        self.cs, self.url = azurite
        sr.apply_migrations(self.engine, MIGRATIONS)
        self.repo = sr.SqlRepository(self.engine, FixedClock(NOW))
        self.repo.upsert_instruments([qs.instrument_row()])
        self.key = self.repo.instrument_keys()[qs.WTI]
        self.service = BlobServiceClient.from_connection_string(self.cs)
        self.container = self.service.get_container_client(layout.CONTAINER_MARKETDATA)
        if not self.container.exists():
            self.container.create_container()
        self.store = AzureBlobStore(self.service)

    def reset(self) -> None:
        """Empty hot tables, watermarks and the container."""
        with self.engine.begin() as conn:
            for table in ("md.bar_hot", "md.tick_hot", "ops.migration_watermark"):
                conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed names
        for blob in list(self.container.list_blobs()):
            self.container.delete_blob(blob.name)

    def insert(self, rows: Sequence["HotRow"]) -> None:
        """Insert rows straight into the hot tables (explicit version and ``ingested_at_ns``)."""
        bars = [r.as_bar(self.key) for r in rows if r.dataset != "ticks"]
        ticks = [r.as_tick(self.key) for r in rows if r.dataset == "ticks"]
        with self.engine.begin() as conn:
            if bars:
                conn.execute(insert(sr.bar_hot), bars)
            if ticks:
                conn.execute(insert(sr.tick_hot), ticks)

    def dump(self) -> "Dump":
        """Copy of the whole state (hot rows, watermarks, blobs with tags) to restore later."""
        with self.engine.connect() as conn:
            rows = {
                t.fullname: [dict(r) for r in conn.execute(t.select()).mappings()]
                for t in (sr.bar_hot, sr.tick_hot, sr.watermark)
            }
        tagger = AzureTagger(self.service)
        blobs = {}
        for b in self.container.list_blobs():
            path = f"{layout.CONTAINER_MARKETDATA}/{b.name}"
            blobs[b.name] = (self.store.read(path), tagger.get_tags(path))
        return Dump(rows, blobs)

    def restore(self, dump: "Dump") -> None:
        """Replace the state with ``dump`` (cheaper than re-running the preparation)."""
        self.reset()
        with self.engine.begin() as conn:
            for t in (sr.bar_hot, sr.tick_hot, sr.watermark):
                if dump.rows[t.fullname]:
                    conn.execute(insert(t), dump.rows[t.fullname])
        tagger = AzureTagger(self.service)
        for name, (data, tags) in dump.blobs.items():
            self.container.get_blob_client(name).upload_blob(data, overwrite=True)
            if tags:
                tagger.set_tags(f"{layout.CONTAINER_MARKETDATA}/{name}", tags)

    def run(self, params: Params, fault: Fault | None = None) -> dict[str, Any]:
        """One in-process run; ``Boom`` propagates."""
        return run_once(self.engine, self.service, params, fault)

    def spawn(
        self, params: Params, fault: Fault | None = None, *, mark: Path | None = None
    ) -> subprocess.Popen[str]:
        """Start the worker script for ``params`` (``fault.mode`` kill / hang)."""
        env = {
            **os.environ,
            "CRASH_OP": params.op, "CRASH_NOW_NS": str(params.now_ns),
            "CRASH_HOT_DAYS": str(params.hot_window_days),
            "CRASH_BATCH_ROWS": str(params.batch_rows),
            "CRASH_CHUNK_ROWS": str(params.delete_chunk_rows),
            "CRASH_MAX_PART_ROWS": str(params.max_part_rows),
            "CRASH_ODBC": self.odbc, "CRASH_AZURITE": self.cs,
        }  # fmt: skip
        if fault is not None:
            env |= {"CRASH_POINT": fault.point, "CRASH_NTH": str(fault.nth),
                    "CRASH_MODE": fault.mode, "CRASH_MARK": str(mark)}  # fmt: skip
        return subprocess.Popen(
            [sys.executable, "-I", str(WORKER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def kill_run(self, params: Params, fault: Fault, tmp: Path) -> None:
        """Run the worker until it dies at ``fault``; assert it was really killed there."""
        mark = tmp / f"mark-{uuid.uuid4().hex}"
        proc = self.spawn(params, replace(fault, mode="kill"), mark=mark)
        out, err = finish(proc)
        assert mark.exists(), f"fault point {fault} was never reached\n{out}\n{err[-2000:]}"
        assert mark.read_text(encoding="ascii") == fault.point
        assert proc.returncode == KILLED_CODE, f"not killed ({proc.returncode})\n{err[-2000:]}"

    def clean_run(self, params: Params, timeout: float = 180) -> dict[str, Any]:
        """Run the worker to completion, retrying while a dying session still holds the lock."""

        def attempt() -> dict[str, Any] | None:
            proc = self.spawn(params)
            out, err = finish(proc)
            assert proc.returncode == 0, f"worker failed\n{out}\n{err[-3000:]}"
            result: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
            return None if result["locked"] else result

        result = _poll("the tiering applock to be free", attempt, timeout, 1)
        assert not result["errors"], result["errors"]
        return result

    def applock_free(self, resource: str) -> bool:
        """Whether an exclusive session lock on ``resource`` could be granted right now."""
        with self.engine.connect() as conn:
            granted = conn.exec_driver_sql(
                "SELECT APPLOCK_TEST('public', ?, 'Exclusive', 'Session')", (resource,)
            ).scalar()
        return granted == 1

    def snapshot(self) -> Snapshot:
        """What "same final state" compares (SPEC-03 §6)."""
        sql: list[Row] = []
        for table in (sr.bar_hot, sr.tick_hot):
            ticks = table is sr.tick_hot
            for r in qs.raw_rows(self.repo, table).iter_rows(named=True):
                interval = None if ticks else _BY_CODE[r["interval_code"]].value
                sql.append(
                    _row("ticks" if ticks else f"bars_{interval}", qs.WTI, interval, r, ticks)
                )
        cold: list[Row] = []
        for dataset in layout.DATASETS:
            for path in self.store.list(f"marketdata/v1/{dataset}/"):
                if not path.endswith(".parquet"):
                    continue
                df = pl.from_arrow(pq.read_table(io.BytesIO(self.store.read(path))))
                assert isinstance(df, pl.DataFrame)
                for r in df.iter_rows(named=True):
                    cold.append(
                        _row(dataset, r["instrument_id"], r.get("interval"), r, dataset == "ticks")
                    )
        return (
            sorted(sql, key=repr),
            sorted(cold, key=repr),
            self.repo.get_watermark("md.bar_hot"),
            self.repo.get_watermark("md.tick_hot"),
        )


@dataclass(frozen=True, slots=True)
class Dump:
    """A saved world state."""

    rows: dict[str, list[dict[str, Any]]]  # table -> rows (SQL column values)
    blobs: dict[str, tuple[bytes, dict[str, str]]]  # name in container -> (bytes, index tags)


_BY_CODE: Final = {code: iv for iv, code in INTERVAL_CODES.items()}


@pytest.fixture(scope="module")
def crash_world(database: tuple[Engine, str], azurite: tuple[str, str]) -> World:
    return World(database, azurite)


@pytest.fixture
def world(crash_world: World) -> World:
    """The module's world, emptied."""
    crash_world.reset()
    return crash_world


def assert_same(got: Snapshot, want: Snapshot, label: str) -> None:
    """Equal snapshots, with a readable diff (the lists can hold thousands of rows)."""
    names = ("SQL rows", "cold rows", "bar watermark", "tick watermark")
    for name, a, b in zip(names, got, want, strict=True):
        if a == b:
            continue
        if isinstance(a, list) and isinstance(b, list):
            extra, missing = [r for r in a if r not in b], [r for r in b if r not in a]
            pytest.fail(
                f"{label}: {name} differ: {len(a)} vs {len(b)}; extra {extra[:5]}; "
                f"missing {missing[:5]}"
            )
        pytest.fail(f"{label}: {name} differ: {a} vs {b}")


# -- scenarios -----------------------------------------------------------------------------------
HOT_DAYS = 30
D_START = T0  # 2019-10-01
NOW_A = D_START + 106 * DAY  # 2020-01-15: first migration, target 2019-12-01
NOW_B = D_START + 166 * DAY  # 2020-03-15: crashing migration, target 2020-02-01
TARGET_A = month_bounds(2019, 12)[0]
TARGET_B = month_bounds(2020, 2)[0]
ING0, ING_GAP, ING_LATE = 1_570_000_000 * 10**9, 10**15, 1_600_000_000 * 10**9
#: dataset -> (first ts, step, number of slots, sides)
AXES: Final = {
    "bars_1d": (D_START, DAY, 140, 1),
    "bars_1h": (D_START + 59 * DAY, HOUR, 96, 1),  # 2019-11-29 .. 12-03: crosses the first target
    "ticks": (D_START + 60 * DAY + 12 * HOUR, HOUR // 2, 96, 3),
}
MAX_CELLS: Final = {"bars_1d": 8, "bars_1h": 6, "ticks": 4}


@dataclass(frozen=True, slots=True)
class Late:
    """A row that reaches SQL after its key was migrated (a restatement below the watermark)."""

    kind: str  # value (drawn) | revert (back to the first value) | equal (same as the latest)
    value: int  # quarter units
    version: str  # one (what the repository files) | next (latest + 1) | gap (latest + 2)


@dataclass(frozen=True, slots=True)
class Cell:
    """One key: its ingest history (distinct consecutive values) and its late rows."""

    dataset: str
    ts: int
    side: int
    history: tuple[int, ...]
    late: tuple[Late, ...]


@dataclass(frozen=True, slots=True)
class Scenario:
    """Data shape plus the knobs that make steps repeat (pages, delete chunks, part rolling)."""

    cells: tuple[Cell, ...]
    batch_rows: int
    chunk_rows: int
    max_part_rows: int


@dataclass(frozen=True, slots=True)
class HotRow:
    """One row of ``md.bar_hot`` / ``md.tick_hot``."""

    dataset: str
    ts: int
    side: int
    version: int
    value: float
    ingested: int

    @property
    def quality(self) -> int:
        """Re-versioned rows carry ``RESTATED`` like the repository's."""
        return int(QualityFlag.RESTATED) if self.version > 1 else 0

    def as_bar(self, key: int) -> dict[str, Any]:  # column -> value of a core INSERT
        """Bar row; volume is NULL on every fifth timestamp (data-contracts §3.1)."""
        return {
            "instrument_key": key, "interval_code": INTERVAL_CODES[Interval(self.dataset[5:])],
            "ts_ns": self.ts, "open": self.value - 0.25, "high": self.value + 0.5,
            "low": self.value - 0.5, "close": self.value,
            "volume": None if self.ts // HOUR % 5 == 0 else 1000.0 + self.value,
            "trade_count": None if self.ts // HOUR % 3 == 0 else int(abs(self.value)) + 1,
            "quality": self.quality, "ingested_at_ns": self.ingested,
            "version": self.version,
        }  # fmt: skip

    def as_tick(self, key: int) -> dict[str, Any]:  # column -> value of a core INSERT
        """Tick row."""
        return {
            "instrument_key": key, "ts_ns": self.ts, "price": self.value, "size": 0.5,
            "side": self.side, "quality": self.quality, "ingested_at_ns": self.ingested,
            "version": self.version,
        }  # fmt: skip


def history_rows(sc: Scenario) -> list[HotRow]:
    """Everything ingested before the first migration (versions ``1..n`` per key)."""
    return [
        HotRow(c.dataset, c.ts, c.side, j + 1, v / 4, ING0 + j * ING_GAP)
        for c in sc.cells
        for j, v in enumerate(c.history)
    ]


def late_rows(sc: Scenario) -> list[HotRow]:
    """Late rows for keys that the first migration already moved (``ts < TARGET_A``)."""
    out: list[HotRow] = []
    for i, c in enumerate(c for c in sc.cells if c.ts < TARGET_A and c.late):
        current, version = c.history[-1], len(c.history)
        for n, late in enumerate(c.late):
            value = {"value": late.value, "revert": c.history[0], "equal": current}[late.kind]
            if n == 0:
                version = {"one": 1, "next": version + 1, "gap": version + 2}[late.version]
            else:
                version += 1
            out.append(HotRow(c.dataset, c.ts, c.side, version, value / 4, ING_LATE + 10 * i + n))
            current = value
    return out


def _distinct(values: Sequence[int]) -> tuple[int, ...]:
    out: list[int] = []
    for v in values:
        out.append(v + 1 if out and v == out[-1] else v)
    return tuple(out)


_quarter = st.integers(-120, 120)  # prices in quarters: exact floats, negative ones included
_late = st.builds(
    Late,
    kind=st.sampled_from(["value", "revert", "equal"]),
    value=_quarter,
    version=st.sampled_from(["one", "next", "gap"]),
)


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """Random data shapes; keys are unique, ingest times strictly increase per key."""
    cells: list[Cell] = []
    for dataset, (start, step, slots, sides) in AXES.items():
        keys = draw(
            st.lists(
                st.tuples(st.integers(0, slots - 1), st.integers(0, sides - 1)),
                unique=True,
                max_size=MAX_CELLS[dataset],
            )
        )
        for slot, side in sorted(keys):
            history = _distinct(draw(st.lists(_quarter, min_size=1, max_size=3)))
            late = tuple(draw(st.one_of(st.just([]), st.lists(_late, min_size=1, max_size=2))))
            cells.append(Cell(dataset, start + slot * step, side, history, late))
    return Scenario(
        tuple(cells),
        batch_rows=draw(st.sampled_from([3, 1000])),
        chunk_rows=draw(st.sampled_from([2, 1000])),
        max_part_rows=draw(st.sampled_from([4, 10_000])),
    )


def _fixed_scenario() -> Scenario:
    """A hand-made shape that reaches every fault point several times."""
    cells: list[Cell] = []
    for slot in range(0, 140, 6):  # daily, two versions on every 4th, late rows on many
        late: tuple[Late, ...] = ()
        if slot % 12 == 0:
            late = (Late("value", slot, "one"),)
        elif slot % 18 == 6:
            late = (Late("equal", 0, "next"),)
        elif slot % 24 == 12:
            late = (Late("value", slot + 7, "next"), Late("revert", 0, "one"))
        hist = (slot - 50, slot - 49, slot - 50) if slot % 4 == 0 else (slot - 50,)
        cells.append(Cell("bars_1d", D_START + slot * DAY, 0, _distinct(hist), late))
    for slot in range(0, 90, 9):
        late = (Late("revert", 0, "next"),) if slot % 18 == 0 else ()
        cells.append(Cell("bars_1h", AXES["bars_1h"][0] + slot * HOUR, 0, (slot, slot + 5), late))
    for slot in range(0, 48, 5):
        late = (Late("value", 3 * slot, "gap"),) if slot % 10 == 0 else ()
        cells.append(Cell("ticks", AXES["ticks"][0] + slot * (HOUR // 2), slot % 3, (slot,), late))
    return Scenario(tuple(cells), batch_rows=4, chunk_rows=3, max_part_rows=5)


FIXED: Final = _fixed_scenario()


def migrate_params(sc: Scenario, now_ns: int) -> Params:
    """The migration of a scenario (``now_ns`` selects the first or the crashing run)."""
    return Params("migrate", now_ns, HOT_DAYS, sc.batch_rows, sc.chunk_rows, sc.max_part_rows)


def compact_params(sc: Scenario) -> Params:
    """The compaction of a scenario."""
    return Params("compact", NOW_B, HOT_DAYS, max_part_rows=sc.max_part_rows)


def prepare(world: World, sc: Scenario) -> None:
    """History in SQL, first migration done (in-process, no fault), late rows inserted."""
    world.reset()
    world.insert(history_rows(sc))
    first = world.run(migrate_params(sc, NOW_A))
    assert not first["errors"], first
    assert not first["locked"]
    world.insert(late_rows(sc))


def _expect_clean(result: dict[str, Any]) -> None:
    assert not result["errors"], result
    assert not result["locked"]


def stages(
    world: World, sc: Scenario, mig: Sequence[Fault] = (), cmp: Sequence[Fault] = ()
) -> tuple[Snapshot, Snapshot, int]:
    """[Crashing] migration, then [crashing] compaction, in-process, from the prepared state.

    Every fault is followed by a re-run; the last run of each stage is fault-free. Returns the
    snapshot after migration, after compaction and the number of faults that fired.
    """
    fired = 0
    snapshots: list[Snapshot] = []
    for params, faults in ((migrate_params(sc, NOW_B), mig), (compact_params(sc), cmp)):
        for fault in faults:
            try:
                world.run(params, fault)
            except Boom:
                fired += 1
        _expect_clean(world.run(params))
        snapshots.append(world.snapshot())
    return snapshots[0], snapshots[1], fired


@dataclass(frozen=True, slots=True)
class Reference:
    """Uninterrupted run of the fixed scenario, with the states the crash tests restart from."""

    prepared: Dump  # first migration done, late rows inserted
    migrated: Dump  # after the second migration
    after_migration: Snapshot
    after_compaction: Snapshot


@pytest.fixture(scope="module")
def reference(crash_world: World) -> Reference:
    """Reference states of :data:`FIXED`."""
    prepare(crash_world, FIXED)
    prepared = crash_world.dump()
    _expect_clean(crash_world.run(migrate_params(FIXED, NOW_B)))
    migrated = crash_world.dump()
    first = crash_world.snapshot()
    _expect_clean(crash_world.run(compact_params(FIXED)))
    sql, cold, wm_bar, wm_tick = first
    assert (wm_bar, wm_tick) == (TARGET_B, TARGET_B)
    assert sql
    assert cold
    assert all(r[1] >= TARGET_B for r in sql)  # SPEC-03 §6 first bullet
    assert len(cold) > len({r[:3] for r in cold})  # several versions per key
    return Reference(prepared, migrated, first, crash_world.snapshot())


# -- 1. real process kills at every step -----------------------------------------------------------
FULL = os.environ.get("TIDAL_TEST_CRASH_FULL") == "1"  # every point at several occurrences
# Each Azurite call costs ~100 ms on Docker Desktop, so a kill + re-run takes about a minute:
# the default matrix kills every step once, in the middle of the run; FULL adds the edges.
MIGRATE_KILLS: Final = [
    ("before_write", 3), ("after_part_put", 2), ("after_write", 2), ("before_tag", 2),
    ("before_manifest", 2), ("manifest_after_delete", 2), ("before_delete", 3),
    ("between_delete_chunks", 3), ("before_watermark", 1), ("after_watermark", 1),
    ("after_watermark", 2),
    *([("before_write", 1), ("after_part_put", 1), ("after_part_put", 5), ("after_write", 1),
       ("before_tag", 1), ("before_manifest", 1), ("manifest_after_delete", 1),
       ("before_delete", 1), ("before_delete", 6), ("between_delete_chunks", 1),
       ("between_delete_chunks", 6)] if FULL else []),
]  # fmt: skip
# ADR-0015: ``before_tag`` is reached only by tagged datasets (ticks), i.e. after the bars have
# been compacted; every other point fires first on bars_1d. The ``mark`` assertion in ``kill_run``
# proves each point was reached.
COMPACT_KILLS: Final = [
    ("before_write", 1), ("after_part_put", 2), ("after_write", 1), ("before_tag", 1),
    ("before_delete", 1), ("between_delete_chunks", 2), ("before_manifest", 1),
    ("manifest_after_delete", 1),
    *([("after_part_put", 1), ("before_write", 2), ("between_delete_chunks", 1),
       ("between_delete_chunks", 4)] if FULL else []),
]  # fmt: skip


def test_fault_points_cover_the_documented_step_order() -> None:
    """Every step in the module docstrings has a fault point (guards against silent renames)."""
    assert {p for p, _ in MIGRATE_KILLS} == set(MIGRATE_POINTS)
    assert {p for p, _ in COMPACT_KILLS} == set(COMPACT_POINTS)


@pytest.mark.parametrize(("point", "nth"), MIGRATE_KILLS, ids=str)
def test_kill_migration_then_rerun_equals_uninterrupted_run(
    world: World, reference: Reference, tmp_path: Path, point: str, nth: int
) -> None:
    world.restore(reference.prepared)
    params = migrate_params(FIXED, NOW_B)
    world.kill_run(params, Fault(point, nth), tmp_path)
    if (point, nth) != ("after_watermark", 2):  # nothing left to do after that one
        # after_watermark#1 leaves tick_hot (rows and watermark) pending
        assert world.snapshot() != reference.after_migration, "the kill left no trace"
    world.clean_run(params)
    assert_same(world.snapshot(), reference.after_migration, f"kill at {point}#{nth}")


@pytest.mark.parametrize(("point", "nth"), COMPACT_KILLS, ids=str)
def test_kill_compaction_then_rerun_equals_uninterrupted_run(
    world: World, reference: Reference, tmp_path: Path, point: str, nth: int
) -> None:
    world.restore(reference.migrated)
    params = compact_params(FIXED)
    world.kill_run(params, Fault(point, nth), tmp_path)
    # compaction keeps the row set, so only the duplicates of written-but-not-yet-deleted parts
    # are visible: not before the first write, not once the old parts are gone
    if point not in {"before_write", "before_manifest", "manifest_after_delete"}:
        assert world.snapshot() != reference.after_compaction, "the kill left no trace"
    world.clean_run(params)
    assert_same(world.snapshot(), reference.after_compaction, f"compaction kill {point}#{nth}")


def test_kill_twice_in_a_row_then_rerun_equals_uninterrupted_run(
    world: World, reference: Reference, tmp_path: Path
) -> None:
    world.restore(reference.prepared)
    params = migrate_params(FIXED, NOW_B)
    world.kill_run(params, Fault("between_delete_chunks", 2), tmp_path)
    world.kill_run(params, Fault("manifest_after_delete", 1), tmp_path)
    world.clean_run(params)
    assert_same(world.snapshot(), reference.after_migration, "two kills")


# -- 2. a killed process's applock is released -----------------------------------------------------
@pytest.mark.parametrize(("op", "point", "resource"), [
    ("migrate", "before_delete", "tiering:md.bar_hot"),
    ("compact", "before_delete", "tiering:md.bar_hot"),
    # ADR-0015: only ticks are tagged, so ``before_tag`` is reached on the tick dataset (lock
    # md.tick_hot) after bars compaction has already released md.bar_hot
    ("compact", "before_tag", "tiering:md.tick_hot"),
])  # fmt: skip
def test_killed_session_releases_the_applock(
    world: World,
    reference: Reference,
    tmp_path: Path,
    op: str,
    point: str,
    resource: str,
) -> None:
    world.restore(reference.prepared if op == "migrate" else reference.migrated)
    params = migrate_params(FIXED, NOW_B) if op == "migrate" else compact_params(FIXED)
    assert world.applock_free(resource)
    mark = tmp_path / "hang"
    proc = world.spawn(params, Fault(point, 1, "hang"), mark=mark)
    try:

        def reached() -> bool | None:
            if proc.poll() is not None:
                pytest.fail(f"worker exited early: {proc.communicate()[1][-2000:]}")
            return True if mark.exists() else None

        _poll("the worker to reach its fault point", reached, 300)
        assert not world.applock_free(resource), "the running job must hold the applock"
        again = world.spawn(params)  # a second run must find the lock taken
        out, err = finish(again)
        assert again.returncode == 0, err[-2000:]
        assert json.loads(out.strip().splitlines()[-1])["locked"] is True
    finally:
        proc.kill()  # TerminateProcess / SIGKILL from outside, no cleanup code runs
        proc.communicate()
    _poll("the dead session's applock", lambda: world.applock_free(resource) or None, 120)
    world.clean_run(params)
    want = reference.after_migration if op == "migrate" else reference.after_compaction
    assert_same(world.snapshot(), want, f"{op}: after lock release")


# -- 3. property: random data x random crash points -------------------------------------------
_faults = st.lists(
    st.builds(
        Fault,
        point=st.sampled_from(MIGRATE_POINTS),
        nth=st.integers(1, 4),
        mode=st.just("raise"),
    ),
    max_size=2,
)
_cfaults = st.lists(
    st.builds(
        Fault,
        point=st.sampled_from(COMPACT_POINTS),
        nth=st.integers(1, 3),
        mode=st.just("raise"),
    ),
    max_size=1,
)


@settings(
    max_examples=CRASH_EXAMPLES,
    deadline=None,
    derandomize=bool(os.environ.get("CI")),  # CI: fixed seed; locally explore, blob printed
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sc=scenarios(), mig=_faults, cmp=_cfaults)
def test_property_crash_then_rerun_equals_uninterrupted_run(
    crash_world: World, sc: Scenario, mig: list[Fault], cmp: list[Fault]
) -> None:
    prepare(crash_world, sc)
    start = crash_world.dump()
    ref_mig, ref_cmp, _ = stages(crash_world, sc)
    crash_world.restore(start)
    got_mig, got_cmp, fired = stages(crash_world, sc, mig, cmp)
    event(f"{fired} crashes fired")
    for fault in [*mig, *cmp]:
        event(f"fault {fault.point}")
    assert_same(got_mig, ref_mig, f"after migration, faults {mig}")
    assert_same(got_cmp, ref_cmp, f"after compaction, faults {cmp}")
    assert all(r[1] >= TARGET_B for r in got_mig[0])  # nothing below the target stays in SQL
    assert got_mig[2] == got_mig[3] == TARGET_B


# -- 4. reader == pure-SQL baseline across the watermark ----------------------------------
def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _restatements(baseline: sr.SqlRepository, every: int, shift: int) -> dict[str, pl.DataFrame]:
    """Late ``version + 1`` rows (price + 100) for every ``every``-th key below the target."""
    out: dict[str, pl.DataFrame] = {}
    for name, table, key in (
        ("bar", sr.bar_hot, ["interval_code", "ts_ns"]),
        ("tick", sr.tick_hot, ["ts_ns", "side"]),
    ):
        latest = qs.raw_rows(baseline, table).sort(*key, "version").unique(key, keep="last")
        pick = latest.sort(*key).filter(pl.col("ts_ns") < TARGET)[shift::every]
        px = ["open", "high", "low", "close"] if name == "bar" else ["price"]
        out[name] = pick.with_columns(
            pl.col(*px) + 100.0,
            (pl.col("version") + 1).alias("version"),
            pl.lit(qs.LATE_INGEST + shift).alias("ingested_at_ns"),
        )
    return out


def _add_restatements(baseline: sr.SqlRepository, hot: Engine, every: int, shift: int) -> None:
    for name, df in _restatements(baseline, every, shift).items():
        table = sr.bar_hot if name == "bar" else sr.tick_hot
        qs.insert_df(baseline.engine, table, df)
        qs.insert_df(hot, table, df)


_WINDOWS: Final = {  # kind -> (first ts, span) of the data in the small world
    "d1": (T0, 150 * DAY),
    "h1": (T0 + 80 * DAY, 7 * 24 * HOUR),
    "m1": (T0 + 91 * DAY + 12 * HOUR, 800 * (HOUR // 60)),
    "ticks": (qs.TICK0, 10 * DAY),
}
_INTERVAL: Final = {"d1": Interval.D1, "h1": Interval.H1, "m1": Interval.M1}
_AS_OF: Final = [None, *(qs.INGEST_BASE + k * qs.ROUND + o for k in range(4) for o in (0, 1))]


@st.composite
def _queries(draw: st.DrawFn) -> tuple[str, int, int, int | None]:
    kind = draw(st.sampled_from(sorted(_WINDOWS)))
    first, span = _WINDOWS[kind]
    pad = span // 10
    start = first - pad + draw(st.integers(0, 1000)) * (span + 2 * pad) // 1000
    end = start + 1 + draw(st.integers(0, 1000)) * span // 1000
    as_of = None if kind == "ticks" else draw(st.sampled_from(_AS_OF))
    return kind, start, end, as_of


def _check_reader(
    reader: UnifiedReader, base: UnifiedReader, query: tuple[str, int, int, int | None]
) -> None:
    kind, start, end, as_of = query
    if kind == "ticks":
        tick_want = _run(base.read_ticks(qs.WTI, start, end))
        tick_got = _run(reader.read_ticks(qs.WTI, start, end))
        assert tick_got.rows == tick_want.rows, f"{query}: ticks differ"
        return
    interval = _INTERVAL[kind]
    want = _run(base.read_bars(qs.WTI, interval, start, end, as_of_ns=as_of))
    got = _run(reader.read_bars(qs.WTI, interval, start, end, as_of_ns=as_of))
    assert got.rows == want.rows, f"{query}: {len(got.rows)} rows vs {len(want.rows)}"


_READER_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    derandomize=bool(os.environ.get("CI")),
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _property_over_queries(reader: UnifiedReader, base: UnifiedReader) -> None:
    @_READER_SETTINGS
    @given(query=_queries())
    def check(query: tuple[str, int, int, int | None]) -> None:
        _check_reader(reader, base, query)

    check()


def _readers(
    world: World, baseline: sr.SqlRepository, tmp: Path
) -> tuple[UnifiedReader, UnifiedReader]:
    empty = tmp / "empty-cold"
    empty.mkdir()
    base = UnifiedReader(baseline, duck.local_session(empty), FixedClock(NOW), SETTINGS)
    session = duck.azure_session(
        account_url=world.url, managed_identity=False, connection_string=world.cs
    )
    return UnifiedReader(world.repo, session, FixedClock(NOW), SETTINGS), base


def test_reader_equals_sql_baseline_before_migration(world: World, tmp_path: Path) -> None:
    baseline = build_small_baseline()
    qs.copy_rows(baseline, world.engine)
    _add_restatements(baseline, world.engine, 7, 0)
    reader, base = _readers(world, baseline, tmp_path)
    _property_over_queries(reader, base)


def test_reader_equals_sql_baseline_after_migration(world: World, tmp_path: Path) -> None:
    baseline = build_small_baseline()
    qs.copy_rows(baseline, world.engine)
    _add_restatements(baseline, world.engine, 7, 0)
    tagger = AzureTagger(world.service)
    results = run_tiering(world.engine, world.store, tagger, FixedClock(NOW), CFG)
    assert all(not r.errors and not r.locked for r in results)
    assert world.repo.get_watermark("md.bar_hot") == TARGET
    # late restatements of migrated keys, below the watermark, reach SQL only
    _add_restatements(baseline, world.engine, 5, 3)
    reader, base = _readers(world, baseline, tmp_path)
    # the equality below is only meaningful if the old rows now live in Blob alone
    old_in_sql = [r for r in world.snapshot()[0] if r[1] < TARGET and r[3] == 1]
    assert not old_in_sql
    spanning = _run(reader.read_bars(qs.WTI, Interval.D1, T0, T0 + 150 * DAY))
    assert spanning.tier is Tier.MIXED
    assert len(spanning.rows) > 100
    _property_over_queries(reader, base)
