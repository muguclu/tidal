"""``SqlBacktestStore`` against a real SQL Server (T-607).

Uses the running compose server (``TIDAL_TEST_SQL_PASSWORD`` or ``TIDAL_SQL__PASSWORD``, optional
``TIDAL_TEST_SQL_URL``, default ``127.0.0.1,1433``) and a throwaway database migrated from
``sql/migrations``. Without a password the tests skip (fail under ``TIDAL_TEST_REQUIRE_SQL=1``/CI).
"""

import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyodbc
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from ulid import ULID

from tidal.api.aggregator import ENQUEUE_FAILED, JobRecord, expire_stale, refresh_parent
from tidal.api.job_store import SqlBacktestStore
from tidal.contracts.models import JobStatus
from tidal.storage.sql_repo import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MIGRATIONS = Path(__file__).resolve().parents[2] / "sql" / "migrations"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
OWNER = "a" * 64


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only."""
    return "asyncio"


def _unavailable(reason: str) -> None:
    if os.environ.get("CI") or os.environ.get("TIDAL_TEST_REQUIRE_SQL") == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def _engine(server: str, password: str, database: str) -> Engine:
    drivers = sorted(
        (d for d in pyodbc.drivers() if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(d.split()[2]),
    )
    if not drivers:
        _unavailable("no 'ODBC Driver N for SQL Server' installed")
    odbc = (
        f"DRIVER={{{drivers[-1]}}};SERVER={server};DATABASE={database};UID=sa;"
        f"PWD={{{password}}};TrustServerCertificate=yes"
    )
    return create_engine(URL.create("mssql+pyodbc", query={"odbc_connect": odbc}))


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A throwaway migrated database on the compose SQL Server."""
    password = os.environ.get("TIDAL_TEST_SQL_PASSWORD") or os.environ.get("TIDAL_SQL__PASSWORD")
    if not password:
        _unavailable("set TIDAL_TEST_SQL_PASSWORD (or TIDAL_SQL__PASSWORD) for the compose server")
        return
    server = os.environ.get("TIDAL_TEST_SQL_URL", "127.0.0.1,1433")
    name = f"tidal_jobs_{uuid.uuid4().hex[:10]}"
    master = _engine(server, password, "master")
    with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE [{name}]"))
    eng = _engine(server, password, name)
    try:
        apply_migrations(eng, MIGRATIONS)
        yield eng
    finally:
        eng.dispose()
        with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"))
            conn.execute(text(f"DROP DATABASE [{name}]"))
        master.dispose()


@pytest.fixture
def store(engine: Engine) -> SqlBacktestStore:
    """A store on the throwaway database."""
    return SqlBacktestStore(engine)


def _id() -> str:
    return f"bt_{ULID()}"


def _sweep(n: int, owner: str = OWNER, submitted: datetime = T0) -> list[JobRecord]:
    parent = _id()
    rows = [
        JobRecord(
            job_id=parent,
            parent_job_id=None,
            child_index=None,
            owner_hash=owner,
            status=JobStatus.QUEUED,
            request_json='{"sweep": 1}',
            submitted_at=submitted,
            children_total=n,
            children_succeeded=0,
            children_failed=0,
        )
    ]
    rows += [
        JobRecord(
            job_id=_id(),
            parent_job_id=parent,
            child_index=i,
            owner_hash=owner,
            status=JobStatus.QUEUED,
            request_json='{"x": "ü"}',
            submitted_at=submitted,
        )
        for i in range(n)
    ]
    return rows


async def _finish(
    store: SqlBacktestStore, job_id: str, status: JobStatus, code: str | None = None
) -> bool:
    ok = status == JobStatus.SUCCEEDED
    return await store.finish_child(
        job_id,
        status=status,
        metrics_json='{"sharpe": 1.5}' if ok else None,
        result_sha256="c" * 64 if ok else None,
        error_code=code,
        finished_at=T0 + timedelta(minutes=1),
    )


async def test_insert_get_roundtrip_and_atomicity(store: SqlBacktestStore) -> None:
    rows = _sweep(3)
    await store.insert(rows)
    got = await store.get(rows[0].job_id)
    assert got == rows[0]  # UTC-aware datetimes, counters, request_json round trip
    child = await store.get(rows[2].job_id)
    assert child is not None
    assert child.request_json == '{"x": "ü"}'
    assert await store.get("bt_" + "0" * 26) is None
    fresh = _sweep(2)  # a duplicate key in the batch rolls the whole batch back
    with pytest.raises(Exception):  # noqa: B017, PT011 - driver IntegrityError
        await store.insert([*fresh, fresh[1]])
    assert await store.get(fresh[0].job_id) is None


async def test_finish_child_first_terminal_wins_and_enqueue_failed_is_overridable(
    store: SqlBacktestStore,
) -> None:
    rows = _sweep(2)
    await store.insert(rows)
    a, b = rows[1].job_id, rows[2].job_id
    assert await _finish(store, a, JobStatus.SUCCEEDED) is True
    assert await _finish(store, a, JobStatus.FAILED, "WORKER_ERROR") is False  # first wins
    got = await store.get(a)
    assert got is not None
    assert got.status == JobStatus.SUCCEEDED
    assert got.metrics_json == '{"sharpe": 1.5}'
    assert await _finish(store, b, JobStatus.FAILED, ENQUEUE_FAILED) is True
    assert await _finish(store, b, JobStatus.SUCCEEDED) is True  # ambiguous send: result wins
    assert await _finish(store, b, JobStatus.FAILED, "X") is False


async def test_counts_and_refresh_parent_derive_status(store: SqlBacktestStore) -> None:
    rows = _sweep(3)
    await store.insert(rows)
    parent = rows[0].job_id
    counts = await store.child_counts(parent)
    assert (counts.total, counts.succeeded, counts.failed, counts.last_finished) == (3, 0, 0, None)
    await _finish(store, rows[1].job_id, JobStatus.SUCCEEDED)
    await _finish(store, rows[2].job_id, JobStatus.SUCCEEDED)
    assert await refresh_parent(store, parent, T0) == JobStatus.RUNNING
    await _finish(store, rows[3].job_id, JobStatus.FAILED, "WORKER_ERROR")
    assert await refresh_parent(store, parent, T0 + timedelta(hours=1)) == JobStatus.PARTIAL
    done = await store.get(parent)
    assert done is not None
    assert (done.status, done.children_succeeded, done.children_failed) == (
        JobStatus.PARTIAL,
        2,
        1,
    )
    assert done.finished_at == T0 + timedelta(minutes=1)  # last child, not "now"


async def test_update_parent_is_monotonic_and_keeps_finished_at(store: SqlBacktestStore) -> None:
    rows = _sweep(2)
    await store.insert(rows)
    parent = rows[0].job_id
    fin = T0 + timedelta(minutes=5)
    await store.update_parent(
        parent, status=JobStatus.SUCCEEDED, succeeded=2, failed=0, finished_at=fin
    )
    # a stale concurrent refresh must not move the counters backwards
    await store.update_parent(
        parent, status=JobStatus.RUNNING, succeeded=1, failed=0, finished_at=None
    )
    got = await store.get(parent)
    assert got is not None
    assert (got.status, got.children_succeeded, got.finished_at) == (JobStatus.SUCCEEDED, 2, fin)
    # an equal-count refresh is allowed but never overwrites a non-null finished_at
    await store.update_parent(
        parent,
        status=JobStatus.SUCCEEDED,
        succeeded=2,
        failed=0,
        finished_at=fin + timedelta(days=1),
    )
    again = await store.get(parent)
    assert again is not None
    assert again.finished_at == fin


async def test_update_parent_same_done_count_keeps_higher_succeeded(
    store: SqlBacktestStore,
) -> None:
    """An ENQUEUE_FAILED override turns a failure into a success at the same done count."""
    rows = _sweep(2)
    await store.insert(rows)
    parent = rows[0].job_id
    fin = T0 + timedelta(minutes=5)
    await store.update_parent(
        parent, status=JobStatus.SUCCEEDED, succeeded=2, failed=0, finished_at=fin
    )
    # stale snapshot taken before the override: same done count (2), one more failure
    await store.update_parent(
        parent, status=JobStatus.PARTIAL, succeeded=1, failed=1, finished_at=fin
    )
    got = await store.get(parent)
    assert got is not None
    assert (got.status, got.children_succeeded, got.children_failed) == (
        JobStatus.SUCCEEDED,
        2,
        0,
    )


async def test_list_parents_keyset_by_owner_status_and_excludes_children(
    store: SqlBacktestStore,
) -> None:
    owner = uuid.uuid4().hex * 2
    jobs = []
    for _ in range(5):
        rows = _sweep(1, owner=owner)
        await store.insert(rows)
        jobs.append(rows[0].job_id)
    await store.insert(_sweep(1, owner="b" * 64))
    newest_first = sorted(jobs, reverse=True)
    page1 = await store.list_parents(owner, None, None, 3)
    assert [r.job_id for r in page1] == newest_first[:3]
    page2 = await store.list_parents(owner, None, page1[-1].job_id, 3)
    assert [r.job_id for r in page2] == newest_first[3:]
    assert await store.list_parents(owner, JobStatus.SUCCEEDED, None, 10) == []
    assert all(r.parent_job_id is None for r in page1 + page2)


async def test_stale_children_and_expire(store: SqlBacktestStore) -> None:
    old = T0 - timedelta(hours=30)
    rows = _sweep(2, submitted=old)
    await store.insert(rows)
    parent = rows[0].job_id
    await _finish(store, rows[1].job_id, JobStatus.SUCCEEDED)
    assert await store.stale_children(parent, T0) == [rows[2].job_id]
    leaf = replace(_sweep(0, submitted=old)[0], children_total=None)
    await store.insert([leaf])
    assert await store.stale_children(leaf.job_id, T0) == [leaf.job_id]
    assert await store.stale_children(leaf.job_id, old - timedelta(hours=1)) == []
    await expire_stale(store, parent, T0 + timedelta(hours=1))
    expired = await store.get(rows[2].job_id)
    assert expired is not None
    assert (expired.status, expired.error_code) == (JobStatus.FAILED, "EXPIRED")
    done = await store.get(parent)
    assert done is not None
    assert done.status == JobStatus.PARTIAL
