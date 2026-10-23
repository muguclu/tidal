"""Backtest result aggregator (T-304): ``backtest-results`` consumer plus the job store seam.

The store is a narrow async protocol over ``ops.backtest_job`` (data-contracts §6); the SQL
implementation is injected (tests use ``tests/fakes/backtest_store``). The aggregator is idempotent
on redelivery (messaging §4): a child already in a terminal state is never counted again, the
first terminal result wins, and the parent status is always recomputed from the children so a
crash between the two steps heals on the next delivery.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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

MAX_DELIVERY: Final = 5  # backtest-results (messaging §1)
_UNKNOWN_SCHEMA: Final = "unknown-schema"


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
        """``UPDATE ... WHERE job_id = ? AND status NOT IN (terminal)``; True if a row changed."""
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
        """Set status and counters; never overwrites a non-null ``finished_at``."""
        ...

    async def list_parents(
        self, owner_hash: str, status: JobStatus | None, offset: int, limit: int
    ) -> list[JobRecord]:
        """Top-level jobs (``parent_job_id IS NULL``) of the owner, newest first."""
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
    max_delivery: int = MAX_DELIVERY

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
