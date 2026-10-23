"""Job handlers: ``backtest.job/v1`` (SPEC-05 §4) and ``tiering.job/v1`` (SPEC-03).

Network paths: the backtest handler gets bars from an injected ``Reader``; on AKS that is the
``tidal-query`` RemoteReader (HTTPS 443 only, the pod cannot reach SQL on 1433). The tiering
handler talks to SQL and Blob directly because it runs as a Container Apps Job (T-309).
"""

import asyncio
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine

from tidal.backtest.engine import BacktestOutput, run_backtest
from tidal.common.errors import NotFound, TidalError, ValidationFailed
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock
from tidal.contracts.messages import (
    QUEUE_BACKTEST_RESULTS,
    SCHEMA_BACKTEST_RESULT,
    BacktestJob,
    BacktestJobMessage,
    BacktestJobResult,
    BacktestResultMessage,
    JobError,
    TieringJobMessage,
)
from tidal.contracts.models import BacktestMetrics
from tidal.contracts.schemas import SCHEMAS, pyarrow_write_kwargs
from tidal.query.protocols import Reader
from tidal.storage import parquet_writer as pw
from tidal.storage.parquet_writer import ObjectStore
from tidal.storage.retag import BlobTagger
from tidal.storage.tiering import run_job
from tidal.worker.consumer import PRODUCER, Outbound, serialise

_log = get_logger(__name__)

SUCCESS_MARKER: Final = "_SUCCESS"


class DataGap(TidalError):  # noqa: N818 - slug-named like tidal.common.errors
    """An instrument has no bars in the requested window (result code ``DATA_GAP``)."""

    status = 422
    slug = "data-gap"
    title = "No data in the requested window"


def _code(exc: TidalError) -> str:
    return exc.slug.upper().replace("-", "_")[:32]


def _write_parquet(store: ObjectStore, path: str, df: pl.DataFrame, schema_name: str) -> None:
    """Write one backtest output file via the storage-layout §3 parquet settings (create-only)."""
    if store.exists(path):
        return  # deterministic content: a previous attempt already wrote it
    table = df.to_arrow().cast(SCHEMAS[schema_name])
    with tempfile.TemporaryDirectory(prefix="tidal-bt-") as tmp:
        local = Path(tmp) / "out.parquet"
        with pq.ParquetWriter(
            local,
            SCHEMAS[schema_name],
            compression=pw.COMPRESSION,
            compression_level=pw.COMPRESSION_LEVEL,
            data_page_size=pw.DATA_PAGE_BYTES,
            write_statistics=True,
            sorting_columns=[pq.SortingColumn(0)],  # ts_ns is column 0
            **pyarrow_write_kwargs(schema_name),  # type: ignore[arg-type]
        ) as writer:
            writer.write_table(table, row_group_size=pw.ROW_GROUP_ROWS)
        store.put_new(path, local)


def _put_bytes(store: ObjectStore, path: str, data: bytes) -> None:
    if store.exists(path):
        return
    with tempfile.TemporaryDirectory(prefix="tidal-bt-") as tmp:
        local = Path(tmp) / "blob"
        local.write_bytes(data)
        store.put_new(path, local)


@dataclass
class BacktestHandler:
    """Loads bars through the unified reader, runs the engine, stores outputs, reports a result."""

    store: ObjectStore
    clock: Clock
    reader: Reader | None = None  # injected (tests: in-process fake)
    #: Fresh reader per job (production: RemoteReader, whose httpx client is loop-bound).
    reader_factory: Callable[[], Reader] | None = None
    node_pool: str = "spot"
    schema: str = "backtest.job/v1"

    def parse(self, body: bytes) -> BacktestJobMessage:
        """Validate the envelope (``pydantic.ValidationError`` on failure)."""
        return BacktestJobMessage.model_validate_json(body)

    # -- data ----------------------------------------------------------------------------------
    async def _load(self, job: BacktestJob) -> dict[str, pl.DataFrame]:
        reader = self.reader_factory() if self.reader_factory is not None else self.reader
        if reader is None:
            raise RuntimeError("BacktestHandler needs a reader or a reader_factory")
        try:
            return await self._load_with(reader, job)
        finally:
            close = getattr(reader, "aclose", None)
            if self.reader_factory is not None and close is not None:
                await close()

    @staticmethod
    async def _load_with(reader: Reader, job: BacktestJob) -> dict[str, pl.DataFrame]:
        req = job.request
        bars: dict[str, pl.DataFrame] = {}
        for inst in req.instrument_ids:
            stream = await reader.stream_bars(inst, req.interval, req.start, req.end)
            parts = [b async for b in stream.batches]
            table = pa.Table.from_batches(parts) if parts else pa.table({})
            if table.num_rows == 0:
                msg = f"no {req.interval.value} bars for {inst} in the requested window"
                raise DataGap(msg)
            bars[inst] = pl.from_arrow(table.select(["ts_ns", "open", "close"]))  # type: ignore[assignment]
        return bars

    # -- outputs -------------------------------------------------------------------------------
    def _result(
        self, job: BacktestJob, corr: str, attempt: int, duration_ms: int, **kw: object
    ) -> Outbound:
        payload = BacktestJobResult(
            job_id=job.job_id,
            parent_job_id=job.parent_job_id,
            duration_ms=duration_ms,
            node_pool=self.node_pool,
            attempt=attempt,
            **kw,  # type: ignore[arg-type]
        )
        env = BacktestResultMessage.model_validate(
            {
                "schema": SCHEMA_BACKTEST_RESULT,
                "message_id": f"{job.job_id}.result",
                "correlation_id": corr,
                "produced_at": self.clock.now(),
                "producer": PRODUCER,
                "payload": payload,
            }
        )
        return Outbound(
            QUEUE_BACKTEST_RESULTS,
            serialise(env),
            env.message_id,
            corr,
            SCHEMA_BACKTEST_RESULT,
        )

    def _store_outputs(self, prefix: str, out: BacktestOutput) -> None:
        _write_parquet(self.store, f"{prefix}equity.parquet", out.equity, "BT_EQUITY_V1")
        _write_parquet(self.store, f"{prefix}trades.parquet", out.trades, "BT_TRADES_V1")
        _put_bytes(self.store, f"{prefix}metrics.json", out.metrics.model_dump_json().encode())
        # marker last: its presence means every other file is complete
        _put_bytes(self.store, f"{prefix}{SUCCESS_MARKER}", out.result_sha256.encode())

    def execute(self, message: object, attempt: int, correlation_id: str) -> list[Outbound]:
        """Run (or replay) one job and return the ``backtest.result/v1`` to send."""
        assert isinstance(message, BacktestJobMessage)  # noqa: S101 - narrowing
        job = message.payload
        prefix = job.output_prefix
        started = time.monotonic()
        marker = f"{prefix}{SUCCESS_MARKER}"
        if self.store.exists(marker):  # redelivery: no recompute, just re-announce the result
            sha = self.store.read(marker).decode().strip()
            metrics = BacktestMetrics.model_validate_json(self.store.read(f"{prefix}metrics.json"))
            _log.info("backtest_replay", job_id=job.job_id)
            return [
                self._result(
                    job,
                    correlation_id,
                    attempt,
                    0,
                    status="succeeded",
                    metrics=metrics,
                    result_sha256=sha,
                )
            ]
        try:
            bars = asyncio.run(self._load(job))
            out = run_backtest(job.request, bars)
            self._store_outputs(prefix, out)
        except (ValidationFailed, NotFound, DataGap) as exc:
            _log.warning("backtest_failed", job_id=job.job_id, code=_code(exc))
            return [
                self._result(
                    job,
                    correlation_id,
                    attempt,
                    int((time.monotonic() - started) * 1000),
                    status="failed",
                    error=JobError(code=_code(exc), detail=exc.detail or exc.title),
                )
            ]
        return [
            self._result(
                job,
                correlation_id,
                attempt,
                int((time.monotonic() - started) * 1000),
                status="succeeded",
                metrics=out.metrics,
                result_sha256=out.result_sha256,
            )
        ]

    def on_exhausted(
        self, message: object, attempt: int, correlation_id: str, error: BaseException
    ) -> list[Outbound]:
        """Last delivery failed: tell the aggregator so the parent does not wait forever."""
        assert isinstance(message, BacktestJobMessage)  # noqa: S101 - narrowing
        detail = f"{type(error).__name__} after {attempt} deliveries"  # no exception text: may leak
        return [
            self._result(
                message.payload,
                correlation_id,
                attempt,
                0,
                status="failed",
                error=JobError(code="INTERNAL", detail=detail),
            )
        ]


@dataclass
class TieringHandler:
    """Validates ``tiering.job/v1`` and calls ``tidal.storage.tiering.run_job``."""

    engine: Engine
    store: ObjectStore
    tagger: BlobTagger
    clock: Clock
    schema: str = "tiering.job/v1"

    def parse(self, body: bytes) -> TieringJobMessage:
        """Validate the envelope (``pydantic.ValidationError`` on failure)."""
        return TieringJobMessage.model_validate_json(body)

    def execute(
        self,
        message: object,
        attempt: int,  # noqa: ARG002 - Handler protocol
        correlation_id: str,  # noqa: ARG002
    ) -> list[Outbound]:
        """Run the tiering job. A held applock (``locked``) is a normal completion.

        Row-level errors reported in ``TableResult.errors`` raise so the message is retried
        (tiering is safe to re-run), then dead-lettered after the last delivery.
        """
        assert isinstance(message, TieringJobMessage)  # noqa: S101 - narrowing
        res = run_job(self.engine, self.store, self.tagger, self.clock, message.payload)
        if res.locked:
            _log.info("tiering_locked", table=res.table, status="locked")
            return []
        if res.errors:
            raise RuntimeError(f"tiering reported {len(res.errors)} error(s)")
        _log.info(
            "tiering_done",
            table=res.table,
            status="dry_run" if res.dry_run else "done",
            rows_moved=res.rows_moved,
            rows_deleted=res.rows_deleted,
        )
        return []

    def on_exhausted(
        self,
        message: object,  # noqa: ARG002 - Handler protocol
        attempt: int,  # noqa: ARG002
        correlation_id: str,  # noqa: ARG002
        error: BaseException,  # noqa: ARG002
    ) -> list[Outbound]:
        """No result queue for tiering: the DLQ entry is the signal."""
        return []
