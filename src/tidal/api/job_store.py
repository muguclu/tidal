# ruff: noqa: S608
# S608: SQL is built only from fixed fragments; every value is a bound parameter.
"""SQL implementation of the ``BacktestStore`` protocol over ``ops.backtest_job`` (T-607).

The engine is the shared synchronous SQLAlchemy engine (``storage.sql_repo``); every call runs in a
worker thread (``asyncio.to_thread``) so the event loop is never blocked. All SQL is parameterised.
The conditional UPDATEs are the idempotency mechanism of the aggregator (messaging-contract §4):
they are decided by the database, not by a read-then-write in Python.

Datetimes: ``DATETIME2(3)`` holds naive UTC. Aware datetimes are converted to UTC on write and
read back as UTC-aware.

Also holds the wiring helpers for the in-process aggregator (SPEC-05 §4: "Result aggregator in
API"): :func:`store_from_settings` and :func:`aggregator_from_settings`.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Row

from tidal.api.aggregator import (
    ENQUEUE_FAILED,
    BacktestStore,
    ChildCounts,
    JobRecord,
    ResultAggregator,
)
from tidal.common.config import Settings
from tidal.common.errors import ConfigError
from tidal.contracts.messages import QUEUE_BACKTEST_RESULTS
from tidal.contracts.models import JobStatus
from tidal.query.protocols import Clock

_COLUMNS = (
    "job_id, parent_job_id, child_index, principal_oid_hash, status, request_json, metrics_json, "
    "result_sha256, error_code, children_total, children_succeeded, children_failed, "
    "submitted_at, finished_at"
)
_TERMINAL = ("succeeded", "failed", "partial")

_INSERT = text(
    "INSERT INTO ops.backtest_job (" + _COLUMNS + ") VALUES (:job_id, :parent_job_id, "
    ":child_index, :owner_hash, :status, :request_json, :metrics_json, :result_sha256, "
    ":error_code, :children_total, :children_succeeded, :children_failed, :submitted_at, "
    ":finished_at)"
)
_GET = text("SELECT " + _COLUMNS + " FROM ops.backtest_job WHERE job_id = :job_id")
_COUNTS = text(
    "SELECT COUNT(*) AS total, "
    "COALESCE(SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), 0) AS succeeded, "
    "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed, "
    "MAX(finished_at) AS last_finished FROM ops.backtest_job WHERE parent_job_id = :parent"
)
_FINISH = text(
    "UPDATE ops.backtest_job SET status = :status, metrics_json = :metrics_json, "
    "result_sha256 = :result_sha256, error_code = :error_code, finished_at = :finished_at "
    "WHERE job_id = :job_id AND (status NOT IN ('succeeded', 'failed', 'partial') "
    "OR (status = 'failed' AND error_code = :enqueue_failed))"
)
_UPDATE_PARENT = text(
    "UPDATE ops.backtest_job SET status = :status, children_succeeded = :succeeded, "
    "children_failed = :failed, finished_at = COALESCE(finished_at, :finished_at) "
    "WHERE job_id = :job_id AND COALESCE(children_succeeded, 0) + COALESCE(children_failed, 0) "
    "<= :done"
)
_STALE = text(
    "SELECT job_id FROM ops.backtest_job WHERE status IN ('queued', 'running') "
    "AND submitted_at < :cutoff AND (parent_job_id = :job_id OR (job_id = :job_id "
    "AND parent_job_id IS NULL AND children_total IS NULL)) ORDER BY job_id"
)


def _naive_utc(value: datetime | None) -> datetime | None:
    """Aware datetime -> naive UTC for ``DATETIME2``; naive input is taken as UTC already."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else value.replace(tzinfo=UTC)


def _record(row: Row[Any]) -> JobRecord:  # Any: SQLAlchemy row
    return JobRecord(
        job_id=row.job_id,
        parent_job_id=row.parent_job_id,
        child_index=row.child_index,
        owner_hash=row.principal_oid_hash,
        status=JobStatus(row.status),
        request_json=row.request_json,
        submitted_at=_aware(row.submitted_at) or datetime.min.replace(tzinfo=UTC),
        children_total=row.children_total,
        children_succeeded=row.children_succeeded,
        children_failed=row.children_failed,
        metrics_json=row.metrics_json,
        result_sha256=row.result_sha256,
        error_code=row.error_code,
        finished_at=_aware(row.finished_at),
    )


class SqlBacktestStore:
    """``BacktestStore`` over a synchronous SQLAlchemy ``Engine`` (owned by the caller)."""

    def __init__(self, engine: Engine) -> None:
        """Wrap ``engine``; its lifetime is the caller's."""
        self._engine = engine

    async def insert(self, records: list[JobRecord]) -> None:
        """Insert parent and children in one transaction (all or nothing)."""
        params = [
            {
                "job_id": r.job_id,
                "parent_job_id": r.parent_job_id,
                "child_index": r.child_index,
                "owner_hash": r.owner_hash,
                "status": r.status.value,
                "request_json": r.request_json,
                "metrics_json": r.metrics_json,
                "result_sha256": r.result_sha256,
                "error_code": r.error_code,
                "children_total": r.children_total,
                "children_succeeded": r.children_succeeded,
                "children_failed": r.children_failed,
                "submitted_at": _naive_utc(r.submitted_at),
                "finished_at": _naive_utc(r.finished_at),
            }
            for r in records
        ]
        if not params:
            return

        def work() -> None:
            with self._engine.begin() as conn:
                conn.execute(_INSERT, params)

        await asyncio.to_thread(work)

    async def get(self, job_id: str) -> JobRecord | None:
        """The row, or ``None``."""

        def work() -> JobRecord | None:
            with self._engine.connect() as conn:
                row = conn.execute(_GET, {"job_id": job_id}).first()
            return None if row is None else _record(row)

        return await asyncio.to_thread(work)

    async def child_counts(self, parent_job_id: str) -> ChildCounts:
        """Count the children of ``parent_job_id`` by terminal status."""

        def work() -> ChildCounts:
            with self._engine.connect() as conn:
                row = conn.execute(_COUNTS, {"parent": parent_job_id}).one()
            return ChildCounts(
                total=int(row.total),
                succeeded=int(row.succeeded),
                failed=int(row.failed),
                last_finished=_aware(row.last_finished),
            )

        return await asyncio.to_thread(work)

    async def finish_child(
        self,
        job_id: str,
        *,
        status: JobStatus,
        metrics_json: str | None,
        result_sha256: str | None,
        error_code: str | None,
        finished_at: datetime,
    ) -> bool:
        """Apply a terminal result once; True if a row changed (ENQUEUE_FAILED is overridable)."""
        params = {
            "job_id": job_id,
            "status": status.value,
            "metrics_json": metrics_json,
            "result_sha256": result_sha256,
            "error_code": error_code,
            "finished_at": _naive_utc(finished_at),
            "enqueue_failed": ENQUEUE_FAILED,
        }

        def work() -> bool:
            with self._engine.begin() as conn:
                return conn.execute(_FINISH, params).rowcount > 0

        return await asyncio.to_thread(work)

    async def update_parent(
        self,
        job_id: str,
        *,
        status: JobStatus,
        succeeded: int,
        failed: int,
        finished_at: datetime | None,
    ) -> None:
        """Set status and counters monotonically; a non-null ``finished_at`` is never replaced."""
        params = {
            "job_id": job_id,
            "status": status.value,
            "succeeded": succeeded,
            "failed": failed,
            "done": succeeded + failed,
            "finished_at": _naive_utc(finished_at),
        }

        def work() -> None:
            with self._engine.begin() as conn:
                conn.execute(_UPDATE_PARENT, params)

        await asyncio.to_thread(work)

    async def list_parents(
        self, owner_hash: str, status: JobStatus | None, before_job_id: str | None, limit: int
    ) -> list[JobRecord]:
        """Top-level jobs of the owner with ``job_id < before_job_id``, newest (ULID) first."""
        clauses = ["parent_job_id IS NULL", "principal_oid_hash = :owner"]
        params: dict[str, object] = {"owner": owner_hash, "limit": limit}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if before_job_id is not None:
            clauses.append("job_id < :before")
            params["before"] = before_job_id
        # Only fixed fragments are concatenated; every value is a bound parameter.
        query = text(
            "SELECT TOP (:limit) "
            + _COLUMNS
            + " FROM ops.backtest_job WHERE "
            + " AND ".join(clauses)
            + " ORDER BY job_id DESC"
        )

        def work() -> list[JobRecord]:
            with self._engine.connect() as conn:
                return [_record(r) for r in conn.execute(query, params)]

        return await asyncio.to_thread(work)

    async def stale_children(self, job_id: str, submitted_before: datetime) -> list[str]:
        """Non-terminal children of ``job_id`` (or the leaf itself) submitted before the cutoff."""
        params = {"job_id": job_id, "cutoff": _naive_utc(submitted_before)}

        def work() -> list[str]:
            with self._engine.connect() as conn:
                return [str(r.job_id) for r in conn.execute(_STALE, params)]

        return await asyncio.to_thread(work)


# -- wiring ------------------------------------------------------------------------------------
def sql_configured(settings: Settings) -> bool:
    """True when a SQL connection can be built (a password locally, Managed Identity elsewhere)."""
    s = settings.sql
    return s.password is not None if s.auth == "sql_password" else True


def store_from_settings(settings: Settings) -> SqlBacktestStore | None:
    """The SQL job store, or ``None`` when SQL is not configured (submit then answers 503)."""
    if not sql_configured(settings):
        return None
    from tidal.storage.sql_repo import create_engine_from_settings  # noqa: PLC0415

    return SqlBacktestStore(create_engine_from_settings(settings))


class OwnedReceiver:
    """A ``Receiver`` that owns its Service Bus client and closes both in :meth:`close`."""

    def __init__(self, receiver: Any, client: Any, credential: Any = None) -> None:  # Any: azure
        """Wrap the azure sync receiver, client and optional credential."""
        self._receiver = receiver
        self._client = client
        self._credential = credential

    def receive_messages(
        self, max_message_count: int | None = None, max_wait_time: float | None = None
    ) -> list[Any]:  # Any: azure messages
        """Delegate to the azure receiver."""
        return list(
            self._receiver.receive_messages(
                max_message_count=max_message_count, max_wait_time=max_wait_time
            )
        )

    def complete_message(self, message: Any) -> None:  # Any: azure message
        """Complete ``message``."""
        self._receiver.complete_message(message)

    def abandon_message(self, message: Any) -> None:  # Any: azure message
        """Abandon ``message``."""
        self._receiver.abandon_message(message)

    def dead_letter_message(
        self,
        message: Any,  # Any: azure message
        reason: str | None = None,
        error_description: str | None = None,
    ) -> None:
        """Dead-letter ``message``."""
        self._receiver.dead_letter_message(
            message, reason=reason, error_description=error_description
        )

    def renew_message_lock(self, message: Any) -> object:  # Any: azure message
        """Renew the lock of ``message``."""
        return self._receiver.renew_message_lock(message)

    def close(self) -> None:
        """Close receiver, client and credential; one failing does not skip the rest."""
        for closer in (
            self._receiver.close,
            self._client.close,
            getattr(self._credential, "close", None),
        ):
            if closer is not None:
                with contextlib.suppress(Exception):  # shutdown path, nothing left to do
                    closer()


def aggregator_from_settings(
    settings: Settings, store: BacktestStore, clock: Clock
) -> ResultAggregator | None:
    """The in-process result aggregator, or ``None`` when Service Bus is not configured.

    Same rules as ``bus_from_settings``: the connection string is local only (``ConfigError``
    elsewhere); outside local the FQDN must be a namespace and the workload's Managed Identity is
    used over AMQP-over-WebSocket (443 only).
    """
    from azure.servicebus import ServiceBusClient, TransportType  # noqa: PLC0415

    sb = settings.servicebus
    credential: Any = None  # Any: azure credential
    if settings.env == "local":
        if sb.connection_string is None:
            return None
        client = ServiceBusClient.from_connection_string(
            sb.connection_string.get_secret_value(), transport_type=TransportType.Amqp
        )
    else:
        if sb.connection_string is not None:
            msg = "TIDAL_SERVICEBUS__CONNECTION_STRING is only allowed when TIDAL_ENV=local"
            raise ConfigError(msg)
        if not sb.fqdn.endswith(".servicebus.windows.net"):
            msg = "TIDAL_SERVICEBUS__FQDN must be a namespace FQDN outside TIDAL_ENV=local"
            raise ConfigError(msg)
        from tidal.api.auth import build_credential  # noqa: PLC0415

        credential = build_credential(aio=False)
        client = ServiceBusClient(
            sb.fqdn, credential, transport_type=TransportType.AmqpOverWebsocket
        )
    receiver = OwnedReceiver(client.get_queue_receiver(QUEUE_BACKTEST_RESULTS), client, credential)
    return ResultAggregator(store=store, clock=clock, receiver=receiver)
