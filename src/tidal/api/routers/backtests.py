"""``/v1/backtests``: submit (with sweep fan-out), list and status (Quant+), T-304.

Submit validates, expands ``sweep`` into child jobs, records parent and children in
``ops.backtest_job`` and then enqueues the children on ``backtest-jobs`` in batches. Records are
written before sending so a result can never arrive for an unknown job. The contract defines no
idempotency key, so a client retry creates a new job.
"""

import hashlib
import itertools
import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from ulid import ULID

from tidal import __version__
from tidal.api.aggregator import (
    ENQUEUE_FAILED,
    BacktestStore,
    JobRecord,
    derive_status,
    expire_stale,
    refresh_parent,
)
from tidal.api.auth_roles import QUANT, require_role
from tidal.api.deps import Principal, get_bus, get_clock
from tidal.api.errors import CORRELATION_HEADER, correlation_header_doc, problem_docs
from tidal.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Position,
    binding,
    cursor_key,
    decode_cursor,
    encode_cursor,
)
from tidal.common.errors import DependencyUnavailable, NotFound, RangeTooLarge, ValidationFailed
from tidal.common.logging import get_correlation_id, get_logger, new_correlation_id
from tidal.contracts.messages import (
    SCHEMA_BACKTEST_JOB,
    BacktestJob,
    BacktestJobMessage,
)
from tidal.contracts.models import (
    MAX_SWEEP_CHILDREN,
    MAX_SWEEP_KEYS,
    BacktestAccepted,
    BacktestChildren,
    BacktestList,
    BacktestMetrics,
    BacktestRequest,
    BacktestResult,
    BacktestStatus,
    JobId,
    JobStatus,
)
from tidal.contracts.paths import backtest_prefix
from tidal.query.protocols import Bus, Clock
from tidal.worker.consumer import MessageTooLarge, serialise

logger = get_logger(__name__)

#: Messages per ``send_batch`` call and body-byte budget per batch (Basic SKU batch cap is 256 KiB).
BATCH_MAX_MESSAGES = 100
BATCH_MAX_BYTES = 200_000
PRODUCER = f"tidal-api@{__version__}"
_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.PARTIAL})

router = APIRouter(prefix="/v1", tags=["backtests"])
_OK_HEADERS: dict[str, Any] = {"headers": {CORRELATION_HEADER: correlation_header_doc()}}
QuantPrincipal = Annotated[Principal, require_role(QUANT)]


def get_store(request: Request) -> BacktestStore:
    """Injected job store (``app.state.backtest_store``); 503 when none is configured."""
    store: BacktestStore | None = getattr(request.app.state, "backtest_store", None)
    if store is None:
        raise DependencyUnavailable("The job store is not configured.")
    return store


async def sweep_guard(request: Request, _principal: QuantPrincipal) -> None:
    """413 for a sweep whose cartesian product exceeds the limit, before body validation (400)."""
    try:
        body = await request.json()
    except ValueError:
        return  # body validation reports it
    sweep = body.get("sweep") if isinstance(body, dict) else None
    if not isinstance(sweep, dict) or not 1 <= len(sweep) <= MAX_SWEEP_KEYS:
        return
    if not all(_is_value_list(v) for v in sweep.values()):
        return  # not a well-formed sweep: body validation answers 400
    if math.prod(len(v) for v in sweep.values()) > MAX_SWEEP_CHILDREN:
        raise RangeTooLarge(f"The sweep expands to more than {MAX_SWEEP_CHILDREN} jobs.")


def _is_value_list(value: object) -> bool:
    """A sweep axis of numbers (bool is not a number); an axis over 200 is still a 413."""
    return (
        isinstance(value, list)
        and len(value) >= 1
        and all(isinstance(x, int | float) and not isinstance(x, bool) for x in value)
    )


def owner_hash(principal: Principal) -> str:
    """sha256 of the Entra oid (never stored or logged raw)."""
    return hashlib.sha256(principal.oid.encode()).hexdigest()


def expand(request: BacktestRequest, job_id: str) -> list[BacktestJob]:
    """Child jobs of ``request`` (cartesian product, sorted keys); one plain job without sweep."""
    req = request.root
    if req.sweep is None:
        return [BacktestJob(job_id=job_id, request=req, output_prefix=backtest_prefix(job_id))]
    keys = sorted(req.sweep)
    base: dict[str, Any] = req.model_dump(mode="python", exclude={"sweep"})  # Any: model dump
    jobs: list[BacktestJob] = []
    for index, combo in enumerate(itertools.product(*(req.sweep[k] for k in keys))):
        child = type(req).model_validate(
            base | {"params": base["params"] | dict(zip(keys, combo, strict=True))}
        )
        jobs.append(
            BacktestJob(
                job_id=f"bt_{ULID()}",
                parent_job_id=job_id,
                child_index=index,
                request=child,
                output_prefix=backtest_prefix(job_id, index),
            )
        )
    return jobs


def _chunks(items: list[tuple[BacktestJobMessage, int]]) -> list[list[BacktestJobMessage]]:
    """Split into batches of at most BATCH_MAX_MESSAGES / BATCH_MAX_BYTES serialised bytes."""
    batches: list[list[BacktestJobMessage]] = []
    current: list[BacktestJobMessage] = []
    size = 0
    for message, n in items:
        if current and (len(current) >= BATCH_MAX_MESSAGES or size + n > BATCH_MAX_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append(message)
        size += n
    if current:
        batches.append(current)
    return batches


@router.post(
    "/backtests",
    operation_id="submitBacktest",
    summary="Submit a backtest (optionally a parameter sweep)",
    status_code=202,
    response_model=BacktestAccepted,
    openapi_extra={"x-tidal-min-role": QUANT},
    responses={202: _OK_HEADERS, **problem_docs(400, 401, 403, 404, 413, 429, 503)},
)
async def submit_backtest(
    body: BacktestRequest,
    principal: QuantPrincipal,
    _guard: Annotated[None, Depends(sweep_guard)],
    bus: Annotated[Bus, Depends(get_bus)],
    store: Annotated[BacktestStore, Depends(get_store)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> BacktestAccepted:
    """Record the job (and its sweep children) and enqueue the children."""
    root = body.root
    now = clock.now()
    job_id = f"bt_{ULID.from_datetime(now)}"
    corr = get_correlation_id() or new_correlation_id()
    jobs = expand(body, job_id)
    messages: list[tuple[BacktestJobMessage, int]] = []
    for job in jobs:
        message = BacktestJobMessage.model_validate(
            {
                "schema": SCHEMA_BACKTEST_JOB,
                "message_id": job.job_id,
                "correlation_id": corr,
                "produced_at": now,
                "producer": PRODUCER,
                "payload": job,
            }
        )
        try:
            messages.append((message, len(serialise(message))))
        except MessageTooLarge as exc:
            raise RangeTooLarge("The job message exceeds the maximum size.") from exc
    owner = owner_hash(principal)
    is_sweep = root.sweep is not None
    records = [
        JobRecord(
            job_id=job.job_id,
            parent_job_id=job.parent_job_id,
            child_index=job.child_index,
            owner_hash=owner,
            status=JobStatus.QUEUED,
            request_json=job.request.model_dump_json(exclude_none=True),
            submitted_at=now,
        )
        for job in jobs
    ]
    if is_sweep:
        parent = JobRecord(
            job_id=job_id,
            parent_job_id=None,
            child_index=None,
            owner_hash=owner,
            status=JobStatus.QUEUED,
            request_json=root.model_dump_json(exclude_none=True),
            submitted_at=now,
            children_total=len(jobs),
        )
        records.insert(0, parent)
    await store.insert(records)
    sent = 0
    try:
        for batch in _chunks(messages):
            if await bus.send_batch(batch) != len(batch):
                raise ConnectionError("short batch send")
            sent += len(batch)
    except Exception as exc:
        logger.error("enqueue_failed", job_id=job_id, sent=sent, error_type=type(exc).__name__)
        await _fail_unsent(store, jobs[sent:], is_sweep, job_id, clock)
        raise DependencyUnavailable("The job could not be queued.") from exc
    logger.info("backtest_submitted", job_id=job_id, children=len(jobs) if is_sweep else 0)
    return BacktestAccepted(job_id=job_id, status=JobStatus.QUEUED)


async def _fail_unsent(
    store: BacktestStore, unsent: list[BacktestJob], is_sweep: bool, job_id: str, clock: Clock
) -> None:
    """Mark jobs that never reached the queue failed so the parent does not stay queued forever."""
    now = clock.now()
    try:
        for job in unsent:
            await store.finish_child(
                job.job_id,
                status=JobStatus.FAILED,
                metrics_json=None,
                result_sha256=None,
                error_code=ENQUEUE_FAILED,
                finished_at=now,
            )
        if is_sweep:
            await refresh_parent(store, job_id, now)
    except Exception:
        logger.exception("enqueue_cleanup_failed", job_id=job_id)


async def _status_of(
    store: BacktestStore, clock: Clock, record: JobRecord, *, with_result: bool
) -> BacktestStatus:
    await expire_stale(store, record.job_id, clock.now())
    record = await store.get(record.job_id) or record
    if record.children_total is not None:  # sweep parent: aggregate live from the children
        counts = await store.child_counts(record.job_id)
        status = derive_status(counts.total, counts.succeeded, counts.failed)
        finished = counts.last_finished if status in _TERMINAL else None
        return BacktestStatus(
            job_id=record.job_id,
            status=status,
            children=BacktestChildren(
                total=counts.total, succeeded=counts.succeeded, failed=counts.failed
            ),
            submitted_at=record.submitted_at,
            finished_at=record.finished_at or finished,
            result=None,  # contract has no sweep result shape (open question)
        )
    ok = record.status == JobStatus.SUCCEEDED
    result = None
    if ok and with_result and record.metrics_json and record.result_sha256:
        prefix = backtest_prefix(record.job_id)
        result = BacktestResult(
            metrics=BacktestMetrics.model_validate_json(record.metrics_json),
            equity_url=f"{prefix}equity.parquet",
            trades_url=f"{prefix}trades.parquet",
            result_sha256=record.result_sha256,
        )
    return BacktestStatus(
        job_id=record.job_id,
        status=record.status,
        children=BacktestChildren(
            total=1, succeeded=int(ok), failed=int(record.status == JobStatus.FAILED)
        ),
        submitted_at=record.submitted_at,
        finished_at=record.finished_at,
        result=result,
    )


def _position_of(job_id: str) -> Position:
    """The ULID of ``job_id`` as an integer: reuses the HMAC cursor, ULIDs sort by time."""
    return Position(int(ULID.from_str(job_id.removeprefix("bt_"))), 0)


def _job_id_of(pos: Position) -> str:
    try:
        return f"bt_{ULID.from_int(pos.ts_ns)}"
    except ValueError:
        raise ValidationFailed("Invalid cursor.") from None


@router.get(
    "/backtests",
    operation_id="listBacktests",
    summary="List the own backtest jobs of the caller",
    response_model=BacktestList,
    openapi_extra={"x-tidal-min-role": QUANT},
    responses={200: _OK_HEADERS, **problem_docs(400, 401, 403, 429, 503)},
)
async def list_backtests(  # noqa: PLR0917 - contract query parameters
    request: Request,
    principal: QuantPrincipal,
    store: Annotated[BacktestStore, Depends(get_store)],
    clock: Annotated[Clock, Depends(get_clock)],
    status: Annotated[JobStatus | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> BacktestList:
    """Newest first; the signed keyset cursor (last job_id) is bound to principal and filter."""
    owner = owner_hash(principal)
    key = cursor_key(request)
    bound = binding("backtests", owner, status.value if status else "")
    before = _job_id_of(decode_cursor(key, bound, cursor)) if cursor else None
    rows = await store.list_parents(owner, status, before, limit + 1)
    items = [await _status_of(store, clock, r, with_result=False) for r in rows[:limit]]
    more = len(rows) > limit
    return BacktestList(
        items=items,
        next_cursor=encode_cursor(key, bound, _position_of(items[-1].job_id)) if more else None,
    )


@router.get(
    "/backtests/{job_id}",
    operation_id="getBacktest",
    summary="Backtest status, with the result when done",
    description="Jobs owned by another principal are reported as 404.",
    response_model=BacktestStatus,
    openapi_extra={"x-tidal-min-role": QUANT},
    responses={200: _OK_HEADERS, **problem_docs(400, 401, 403, 404, 429, 503)},
)
async def get_backtest(
    job_id: JobId,
    principal: QuantPrincipal,
    store: Annotated[BacktestStore, Depends(get_store)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> BacktestStatus:
    """Parent status aggregated from the children; foreign and child ids are 404."""
    record = await store.get(job_id)
    if record is None or record.owner_hash != owner_hash(principal) or record.parent_job_id:
        raise NotFound("Backtest job not found.")
    return await _status_of(store, clock, record, with_result=True)
