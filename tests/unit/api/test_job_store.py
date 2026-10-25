# ruff: noqa: DTZ001  # naive datetimes are the point of the DATETIME2 conversion tests
"""SQL job store (T-607): statement shape and parameter binding, wiring, aggregator lifespan.

The T-SQL semantics (conditional UPDATEs, TOP, DATETIME2) run against a real SQL Server in
``tests/integration/test_job_store.py``; here a recording engine proves every value is bound (never
interpolated) and that the in-process aggregator starts and stops with the app.
"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from tidal.api import job_store as js
from tidal.api.aggregator import ENQUEUE_FAILED, JobRecord
from tidal.api.app import create_app
from tidal.common.config import ServiceBusSettings, Settings
from tidal.common.errors import ConfigError
from tidal.contracts.models import JobStatus

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only."""
    return "asyncio"


class _Result:
    rowcount = 1

    def __iter__(self) -> Any:
        return iter(())


class _Conn:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.log.append((str(stmt), params))
        return _Result()


class _Engine:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.begins = 0

    def begin(self) -> _Conn:
        self.begins += 1
        return _Conn(self.log)

    def connect(self) -> _Conn:
        return _Conn(self.log)


def _record(job_id: str, **kw: Any) -> JobRecord:
    base: dict[str, Any] = {
        "job_id": job_id,
        "parent_job_id": None,
        "child_index": None,
        "owner_hash": "h" * 64,
        "status": JobStatus.QUEUED,
        "request_json": "{}",
        "submitted_at": datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2))),
    }
    return JobRecord(**{**base, **kw})


async def test_insert_is_one_transaction_with_bound_naive_utc_params() -> None:
    engine = _Engine()
    store = js.SqlBacktestStore(engine)  # type: ignore[arg-type]
    await store.insert([_record("bt_" + "A" * 26), _record("bt_" + "B" * 26)])
    assert engine.begins == 1
    stmt, params = engine.log[0]
    assert "INSERT INTO ops.backtest_job" in stmt
    assert len(params) == 2
    assert params[0]["submitted_at"] == datetime(2026, 1, 1, 10)  # naive UTC
    assert params[0]["status"] == "queued"


async def test_insert_of_nothing_opens_no_transaction() -> None:
    engine = _Engine()
    await js.SqlBacktestStore(engine).insert([])  # type: ignore[arg-type]
    assert engine.begins == 0


async def test_finish_child_is_conditional_and_overrides_only_enqueue_failed() -> None:
    engine = _Engine()
    store = js.SqlBacktestStore(engine)  # type: ignore[arg-type]
    changed = await store.finish_child(
        "bt_x",
        status=JobStatus.FAILED,
        metrics_json=None,
        result_sha256=None,
        error_code="X'; DROP TABLE t;--",
        finished_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert changed is True
    stmt, params = engine.log[0]
    assert "status NOT IN ('succeeded', 'failed', 'partial')" in stmt
    assert "error_code = :enqueue_failed" in stmt
    assert params["enqueue_failed"] == ENQUEUE_FAILED
    assert "DROP TABLE" not in stmt  # values are bound, never interpolated
    assert params["error_code"] == "X'; DROP TABLE t;--"


async def test_update_parent_is_monotonic_and_keeps_finished_at() -> None:
    engine = _Engine()
    store = js.SqlBacktestStore(engine)  # type: ignore[arg-type]
    await store.update_parent(
        "bt_p", status=JobStatus.RUNNING, succeeded=2, failed=1, finished_at=None
    )
    stmt, params = engine.log[0]
    assert "finished_at = COALESCE(finished_at, :finished_at)" in stmt
    assert "<= :done" in stmt
    assert params["done"] == 3


async def test_list_parents_is_keyset_and_parameterised() -> None:
    engine = _Engine()
    store = js.SqlBacktestStore(engine)  # type: ignore[arg-type]
    await store.list_parents("o" * 64, JobStatus.SUCCEEDED, "bt_" + "Z" * 26, 11)
    stmt, params = engine.log[0]
    assert "parent_job_id IS NULL" in stmt
    assert "job_id < :before" in stmt
    assert stmt.rstrip().endswith("ORDER BY job_id DESC")
    assert params["limit"] == 11
    assert "Z" * 26 not in stmt


def test_store_is_none_without_a_sql_password() -> None:
    settings = Settings.from_env({"TIDAL_ENV": "local"})
    assert js.store_from_settings(settings) is None


def test_aggregator_needs_a_connection_string_locally_and_forbids_it_elsewhere() -> None:
    local = Settings.from_env({"TIDAL_ENV": "local"})
    assert js.aggregator_from_settings(local, object(), object()) is None  # type: ignore[arg-type]
    dev = local.model_copy(  # bypasses the config validator: the producer guards it too
        update={
            "env": "dev",
            "servicebus": ServiceBusSettings(connection_string=SecretStr("Endpoint=sb://x/")),
        }
    )
    with pytest.raises(ConfigError, match="only allowed when TIDAL_ENV=local"):
        js.aggregator_from_settings(dev, object(), object())  # type: ignore[arg-type]


class _Receiver:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Aggregator:
    def __init__(self) -> None:
        self.receiver = _Receiver()
        self.started = asyncio.Event()

    async def run(self, *, max_messages: int = 0, max_wait_s: float = 30.0) -> int:
        assert max_messages == 0
        self.started.set()
        await asyncio.sleep(3600)
        return 0


async def test_lifespan_runs_the_aggregator_and_closes_it_on_shutdown() -> None:
    agg = _Aggregator()
    settings = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
    app = create_app(settings, aggregator=agg)  # type: ignore[arg-type]
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(agg.started.wait(), 2)
        assert not agg.receiver.closed
    assert agg.receiver.closed


async def test_aggregator_failure_is_retried_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    import tidal.api.app as app_module  # noqa: PLC0415

    monkeypatch.setattr(app_module, "AGGREGATOR_RETRY_S", 0.01)
    calls = 0

    class Flaky:
        async def run(self, *, max_wait_s: float) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("broker down")
            await asyncio.sleep(3600)
            return 0

    task = asyncio.create_task(app_module.run_aggregator(Flaky()))  # type: ignore[arg-type]
    for _ in range(100):
        if calls >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == 2


def test_naive_utc_conversion() -> None:
    assert js._naive_utc(None) is None
    assert js._naive_utc(datetime(2026, 1, 1, 0, tzinfo=timezone(timedelta(hours=-5)))) == datetime(
        2026, 1, 1, 5
    )
    assert js._aware(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)
