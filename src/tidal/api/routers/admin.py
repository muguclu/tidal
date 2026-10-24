"""``/v1/admin/*``: trigger the hot-to-cold migration and read storage statistics (T-410).

Admin is a separate role (no data roles implied). ``POST /tiering/run`` takes no body (the contract
defines none): it enqueues one ``tiering.job/v1`` per hot table on ``tiering-jobs`` using the
configured hot window. ``GET /storage/stats`` reads the last stats run from an injectable
:class:`StatsSource` (``app.state.storage_stats``). Nothing persists stats yet, so without a source
or before the first run the route answers 503, the only non-auth failure the contract documents.
"""

from typing import Annotated, Any, Final, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, Request
from ulid import ULID

from tidal import __version__
from tidal.api.auth_roles import ADMIN, require_role
from tidal.api.deps import Principal, get_bus, get_clock
from tidal.api.errors import CORRELATION_HEADER, correlation_header_doc, problem_docs
from tidal.common.errors import DependencyUnavailable
from tidal.common.logging import get_correlation_id, get_logger, new_correlation_id
from tidal.contracts.messages import SCHEMA_TIERING_JOB, TieringJob, TieringJobMessage
from tidal.contracts.models import JobStatus, StorageStats, TieringRunAccepted
from tidal.query.protocols import Bus, Clock

logger = get_logger(__name__)

PRODUCER: Final = f"tidal-api@{__version__}"
HOT_TABLES: Final[tuple[Literal["md.bar_hot", "md.tick_hot"], ...]] = ("md.bar_hot", "md.tick_hot")
#: Rows per SQL read page of a triggered run (messaging-contract example value).
BATCH_ROWS: Final = 500_000

router = APIRouter(prefix="/v1/admin", tags=["admin"])
_OK_HEADERS: dict[str, Any] = {"headers": {CORRELATION_HEADER: correlation_header_doc()}}
AdminPrincipal = Annotated[Principal, require_role(ADMIN)]


@runtime_checkable
class StatsSource(Protocol):
    """Reader of the last storage stats run."""

    async def latest(self) -> StorageStats | None:
        """The last snapshot, or ``None`` when no stats run has completed yet."""
        ...


def get_stats_source(request: Request) -> StatsSource:
    """Injected stats source (``app.state.storage_stats``); 503 when none is configured."""
    source: StatsSource | None = getattr(request.app.state, "storage_stats", None)
    if source is None:
        raise DependencyUnavailable("The storage statistics source is not configured.")
    return source


@router.post(
    "/tiering/run",
    operation_id="runTiering",
    summary="Trigger the hot-to-cold migration job (enqueues)",
    status_code=202,
    response_model=TieringRunAccepted,
    openapi_extra={"x-tidal-min-role": ADMIN},
    responses={202: _OK_HEADERS, **problem_docs(401, 403, 429, 503)},
)
async def run_tiering(
    request: Request,
    _principal: AdminPrincipal,
    bus: Annotated[Bus, Depends(get_bus)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> TieringRunAccepted:
    """Enqueue one migration job per hot table; ``run_id`` prefixes both message ids."""
    now = clock.now()
    run_id = f"tr_{ULID.from_datetime(now)}"
    corr = get_correlation_id() or new_correlation_id()
    window: int = request.app.state.settings.hot_window_days
    messages = [
        TieringJobMessage.model_validate(
            {
                "schema": SCHEMA_TIERING_JOB,
                "message_id": f"{run_id}:{table}",
                "correlation_id": corr,
                "produced_at": now,
                "producer": PRODUCER,
                "payload": TieringJob(
                    table=table, hot_window_days=window, dry_run=False, batch_rows=BATCH_ROWS
                ),
            }
        )
        for table in HOT_TABLES
    ]
    try:
        if await bus.send_batch(messages) != len(messages):
            raise ConnectionError("short batch send")
    except Exception as exc:
        logger.error("tiering_enqueue_failed", run_id=run_id, error_type=type(exc).__name__)
        raise DependencyUnavailable("The tiering job could not be queued.") from exc
    logger.info("tiering_enqueued", run_id=run_id, tables=len(messages))
    return TieringRunAccepted(run_id=run_id, status=JobStatus.QUEUED)


@router.get(
    "/storage/stats",
    operation_id="getStorageStats",
    summary="Rows and bytes per tier, from the last stats run",
    response_model=StorageStats,
    openapi_extra={"x-tidal-min-role": ADMIN},
    responses={200: _OK_HEADERS, **problem_docs(401, 403, 429, 503)},
)
async def get_storage_stats(
    _principal: AdminPrincipal,
    source: Annotated[StatsSource, Depends(get_stats_source)],
) -> StorageStats:
    """The last snapshot; 503 until a stats run exists."""
    stats = await source.latest()
    if stats is None:
        raise DependencyUnavailable("No storage statistics run has completed yet.")
    return stats
