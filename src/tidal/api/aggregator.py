"""Backtest result aggregator (T-304): ``backtest-results`` consumer plus the job store seam.

The store is a narrow async protocol over ``ops.backtest_job`` (data-contracts §6); the SQL
implementation is injected (tests use ``tests/fakes/backtest_store``). The aggregator is idempotent
on redelivery (messaging §4): a child already in a terminal state is never counted again, the
first terminal result wins, and the parent status is always recomputed from the children so a
crash between the two steps heals on the next delivery.

Parent status: all children succeeded -> ``succeeded``; all failed -> ``failed``; a mix once every
child is terminal -> ``partial``; some terminal -> ``running``; none -> ``queued``.
``update_parent`` is monotonic (a stale concurrent refresh cannot move the counters backwards).

Expiry: the router calls :func:`expire_stale` on reads. A non-terminal child older than the
``backtest-jobs`` TTL (24 h, messaging §1) plus a 1 h margin can no longer be delivered, so it is
reported ``failed`` / ``EXPIRED`` and persisted. This also covers a crash between the store insert
and the send, and a swallowed error while marking unsent children after a failed send. The one
exception to "first terminal result wins": a worker result overrides ``ENQUEUE_FAILED``, because a
send that raised may still have reached the broker (ambiguous send).
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from pydantic import ValidationError

from tidal.common.logging import correlation_scope, get_logger
from tidal.contracts.messages import (
    MAX_MESSAGE_BYTES,
    SCHEMA_BACKTEST_RESULT,
    BacktestJobResult,
    BacktestResultMessage,
)
from tidal.contracts.models import BacktestMetrics, JobStatus
from tidal.query.protocols import Clock
from tidal.worker.consumer import ReceivedMessage, Receiver, Verdict, validation_summary

_log = get_logger(__name__)

_UNKNOWN_SCHEMA: Final = "unknown-schema"
ENQUEUE_FAILED: Final = "ENQUEUE_FAILED"
EXPIRED: Final = "EXPIRED"
#: backtest-jobs TTL (24 h) plus a margin: after this a queued job can no longer run.
JOB_EXPIRY: Final = timedelta(hours=25)


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One ``ops.backtest_job`` row (a sweep parent, a sweep child or a plain single job)."""

    job_id: str
    parent_job_id: str | None
    child_index: int | None
    owner_hash: str  # sha256 of the Entra oid, never the raw oid
    status: JobStatus
    request_json: str
    submitted_at: datetime
    children_total: int | None = None  # set on sweep parents only
    children_succeeded: int | None = None
    children_failed: int | None = None
    metrics_json: str | None = None
    result_sha256: str | None = None
    error_code: str | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChildCounts:
    """Aggregate of a parent's children."""

    total: int
    succeeded: int
    failed: int
    last_finished: datetime | None = None


class BacktestStore(Protocol):
    """Persistence of ``ops.backtest_job``."""

    async def insert(self, records: list[JobRecord]) -> None:
        """Insert parent and children atomically."""
        ...

    async def get(self, job_id: str) -> JobRecord | None:
        """The row, or ``None``."""
        ...

    async def child_counts(self, parent_job_id: str) -> ChildCounts:
        """Count the children of ``parent_job_id`` by terminal status."""
        ...

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
        """Apply a terminal result once; True if a row changed.

        SQL form: ``UPDATE ... WHERE job_id = :id AND (status NOT IN (terminal) OR
        (status = 'failed' AND error_code = 'ENQUEUE_FAILED'))``.
        """
        ...

    async def update_parent(
        self,
        job_id: str,
        *,
        status: JobStatus,
        succeeded: int,
        failed: int,
        finished_at: datetime | None,
    ) -> None:
        """Set status and counters, monotonically; never overwrites a non-null ``finished_at``.

        Writes only if the new done count (succeeded + failed) and the new succeeded count are
        both >= the stored ones (an ENQUEUE_FAILED override turns a failure into a success at the
        same done count, so a stale snapshot must not win); SQL form:
        ``UPDATE ... WHERE job_id = :id AND COALESCE(children_succeeded, 0) +
        COALESCE(children_failed, 0) <= :done AND COALESCE(children_succeeded, 0) <= :succeeded``.
        """
        ...

    async def list_parents(
        self, owner_hash: str, status: JobStatus | None, before_job_id: str | None, limit: int
    ) -> list[JobRecord]:
        """Top-level jobs of the owner with ``job_id < before_job_id``, newest (ULID) first."""
        ...

    async def stale_children(self, job_id: str, submitted_before: datetime) -> list[str]:
        """Non-terminal jobs submitted before the cutoff: the children of ``job_id``, or the
        job itself when it is a leaf (``parent_job_id IS NULL`` and no children)."""
        ...


def derive_status(total: int, succeeded: int, failed: int) -> JobStatus:
    """Parent status from child counters."""
    done = succeeded + failed
    if done == 0:
        return JobStatus.QUEUED
    if done < total:
        return JobStatus.RUNNING
    if failed == 0:
        return JobStatus.SUCCEEDED
    return JobStatus.FAILED if succeeded == 0 else JobStatus.PARTIAL


async def refresh_parent(store: BacktestStore, parent_job_id: str, now: datetime) -> JobStatus:
    """Recompute the parent from its children (idempotent) and persist it."""
    counts = await store.child_counts(parent_job_id)
    status = derive_status(counts.total, counts.succeeded, counts.failed)
    done = counts.succeeded + counts.failed == counts.total
    await store.update_parent(
        parent_job_id,
        status=status,
        succeeded=counts.succeeded,
        failed=counts.failed,
        finished_at=(counts.last_finished or now) if done else None,
    )
    return status


async def expire_stale(store: BacktestStore, job_id: str, now: datetime) -> None:
    """Fail jobs that sat non-terminal beyond :data:`JOB_EXPIRY` (``EXPIRED``) and refresh."""
    stale = await store.stale_children(job_id, now - JOB_EXPIRY)
    for stale_id in stale:
        await store.finish_child(
            stale_id,
            status=JobStatus.FAILED,
            metrics_json=None,
            result_sha256=None,
            error_code=EXPIRED,
            finished_at=now,
        )
    record = await store.get(job_id)
    if stale and record is not None and record.children_total is not None:
        await refresh_parent(store, job_id, now)


def _fill_metrics(data: dict[str, Any]) -> None:  # Any: JSON dict
    """Inverse of the worker's ``exclude_none`` restore: absent nullable metrics are null."""
    payload = data.get("payload")
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if isinstance(metrics, dict):
        for key in BacktestMetrics.model_fields:
            if key != "n_trades":
                metrics.setdefault(key, None)


@dataclass
class ResultAggregator:
    """Consumes ``backtest-results``; one :meth:`process` call settles one message."""

    store: BacktestStore
    clock: Clock
    receiver: Receiver

    async def _settle(
        self,
        action: Callable[..., object],
        msg: object,
        verdict: Verdict,
        **kwargs: str,
    ) -> Verdict:
        try:
            await asyncio.to_thread(action, msg, **kwargs)
        except Exception:
            _log.exception("settle_failed")
            return Verdict.LOCK_LOST
        return verdict

    async def _dead_letter(self, msg: object, reason: str, description: str) -> Verdict:
        _log.warning("dead_letter", reason=reason)
        return await self._settle(
            self.receiver.dead_letter_message,
            msg,
            Verdict.DEAD_LETTERED,
            reason=reason,
            error_description=description[:2048],
        )

    @staticmethod
    def _parse(body: bytes) -> BacktestResultMessage:
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_BACKTEST_RESULT:
            raise ValueError(_UNKNOWN_SCHEMA)
        _fill_metrics(data)
        return BacktestResultMessage.model_validate(data)

    async def process(self, msg: ReceivedMessage) -> Verdict:
        """Apply one result message and settle it; never raises for message-level problems."""
        body = b"".join(bytes(part) for part in msg.body)
        corr = msg.correlation_id or msg.message_id or "unknown"
        with correlation_scope(corr):
            if len(body) > MAX_MESSAGE_BYTES:
                return await self._dead_letter(msg, "message-too-large", f"{len(body)} bytes")
            try:
                env = self._parse(body)
            except ValidationError as exc:
                return await self._dead_letter(msg, "invalid-message", validation_summary(exc))
            except ValueError as exc:  # json.JSONDecodeError or the schema check
                reason = _UNKNOWN_SCHEMA if str(exc) == _UNKNOWN_SCHEMA else "invalid-json"
                return await self._dead_letter(msg, reason, reason)
            try:
                known = await self._apply(env.payload)
            except Exception:
                _log.exception("aggregate_failed", job_id=env.payload.job_id)
                return await self._settle(self.receiver.abandon_message, msg, Verdict.ABANDONED)
            if not known:
                return await self._dead_letter(msg, "unknown-job", "no such job")
            return await self._settle(self.receiver.complete_message, msg, Verdict.COMPLETED)

    async def _apply(self, result: BacktestJobResult) -> bool:
        """Record the child's first terminal result; False if the job is unknown/inconsistent."""
        record = await self.store.get(result.job_id)
        if record is None or record.parent_job_id != result.parent_job_id:
            return False
        now = self.clock.now()
        applied = await self.store.finish_child(
            result.job_id,
            status=JobStatus.SUCCEEDED if result.status == "succeeded" else JobStatus.FAILED,
            metrics_json=result.metrics.model_dump_json() if result.metrics else None,
            result_sha256=result.result_sha256,
            error_code=result.error.code if result.error else None,
            finished_at=now,
        )
        _log.info("result_applied" if applied else "result_duplicate", job_id=result.job_id)
        if result.parent_job_id is not None:  # also on duplicates: heals a lost parent update
            await refresh_parent(self.store, result.parent_job_id, now)
        return True

    async def run(self, *, max_messages: int = 0, max_wait_s: float = 30.0) -> int:
        """Receive and process until ``max_messages`` (0 = unlimited) or the queue is idle.

        Returns the number of messages handled. Cancel the task to stop.
        """
        handled = 0
        while max_messages == 0 or handled < max_messages:
            batch = await asyncio.to_thread(
                self.receiver.receive_messages, max_message_count=10, max_wait_time=max_wait_s
            )
            if not batch:
                if max_messages:
                    break
                continue
            for msg in batch:
                await self.process(msg)
                handled += 1
        return handled
