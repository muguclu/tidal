"""``tidal-worker`` entry point: one queue, ``--max-messages`` messages, then exit.

``tidal-worker [--queue backtest-jobs|tiering-jobs] [--max-messages N] [--max-wait S]``.
KEDA ScaledJob pods use the defaults (one message, exit when the queue is empty);
``--max-messages 0`` runs until SIGTERM. SIGTERM abandons the in-flight message and exits 143
(spot eviction).

Network paths: backtest pods (AKS, egress 443 + DNS only) use Service Bus over WebSocket and read
bars via ``tidal-query`` (RemoteReader, managed identity); tiering runs as a Container Apps Job
with direct SQL + Blob access.
"""

import argparse
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import ExitStack
from types import FrameType

from tidal.common.config import Settings, get_settings
from tidal.common.logging import configure_logging, get_logger
from tidal.common.telemetry import (
    configure_telemetry,
    message_span,
    record_backtest_duration,
    record_cold_start,
    record_queue_lag,
)
from tidal.common.timeutil import SystemClock, clock_from_settings
from tidal.contracts.messages import (
    QUEUE_BACKTEST_JOBS,
    QUEUE_BACKTEST_RESULTS,
    QUEUE_TIERING_JOBS,
)
from tidal.worker.consumer import Handler, ReceivedMessage, Verdict, Worker
from tidal.worker.dlq import BusClient, client_from_settings
from tidal.worker.handlers import BacktestHandler, TieringHandler

_log = get_logger(__name__)
MAX_DELIVERY = {QUEUE_BACKTEST_JOBS: 3, QUEUE_TIERING_JOBS: 3}


class TracedWorker(Worker):
    """``Worker`` with a CONSUMER span per message (linked to the producer's ``traceparent``)."""

    queue = ""

    def process(self, msg: ReceivedMessage) -> Verdict:
        """Trace, time and measure queue lag for one message, then delegate."""
        props = getattr(msg, "application_properties", None)
        enqueued = getattr(msg, "enqueued_time_utc", None)
        record_cold_start("worker")
        if enqueued is not None:
            record_queue_lag((SystemClock().now() - enqueued).total_seconds() * 1000, self.queue)
        started = time.monotonic()
        with message_span(
            f"{self.queue} process", props, **{"messaging.destination.name": self.queue}
        ):
            verdict = super().process(msg)
        if self.queue == QUEUE_BACKTEST_JOBS:
            record_backtest_duration((time.monotonic() - started) * 1000, verdict.value)
        return verdict


def build_handler(queue: str, settings: Settings) -> Handler:
    """Wire the production dependencies of the handler for ``queue``."""
    from tidal.ingest.cli import build_store  # noqa: PLC0415 - heavy imports only when used

    store = build_store(settings)
    clock = clock_from_settings(settings)
    if queue == QUEUE_TIERING_JOBS:
        # Tiering needs SQL (1433) + Blob directly: it runs as a Container Apps Job (T-309).
        from tidal.storage.parquet_writer import AzureBlobStore, LocalStore  # noqa: PLC0415
        from tidal.storage.retag import AzureTagger, BlobTagger, LocalTagger  # noqa: PLC0415
        from tidal.storage.sql_repo import create_engine_from_settings  # noqa: PLC0415

        tagger: BlobTagger
        if isinstance(store, AzureBlobStore):
            tagger = AzureTagger(store.service)
        elif isinstance(store, LocalStore):
            tagger = LocalTagger(store.root)
        else:
            raise TypeError("unsupported object store")
        return TieringHandler(create_engine_from_settings(settings), store, tagger, clock)
    from tidal.query.service import remote_reader_from_settings  # noqa: PLC0415

    # Backtest pods (AKS) reach only 443: bars come through tidal-query, never SQL directly.
    return BacktestHandler(
        store, clock=clock, reader_factory=lambda: remote_reader_from_settings(settings)
    )


def run_worker(
    client: BusClient,
    handler: Handler,
    queue: str,
    max_messages: int = 1,
    max_wait_s: float = 30.0,
) -> int:
    """Consume ``queue`` with ``client``; returns the exit code (0, or 143 after SIGTERM)."""
    with ExitStack() as stack:
        receiver = stack.enter_context(client.get_queue_receiver(queue))
        senders = {}
        if queue == QUEUE_BACKTEST_JOBS:  # the tiering identity needs no Send right on results
            senders[QUEUE_BACKTEST_RESULTS] = stack.enter_context(
                client.get_queue_sender(QUEUE_BACKTEST_RESULTS)
            )
        worker = TracedWorker(receiver, handler, senders, max_delivery=MAX_DELIVERY.get(queue, 3))
        worker.queue = queue

        def on_term(_signum: int, _frame: FrameType | None) -> None:
            worker.request_stop()

        signal.signal(signal.SIGTERM, on_term)
        return worker.run(max_messages, max_wait_s)


def run(argv: Sequence[str] | None = None) -> None:
    """Console-script entry point."""
    parser = argparse.ArgumentParser(prog="tidal-worker")
    parser.add_argument("--queue", choices=[QUEUE_BACKTEST_JOBS, QUEUE_TIERING_JOBS])
    parser.add_argument("--max-messages", type=int, default=1)
    parser.add_argument("--max-wait", type=float, default=30.0)
    args = parser.parse_args(argv)
    queue = args.queue or QUEUE_BACKTEST_JOBS
    settings = get_settings()
    configure_logging(settings)
    configure_telemetry(settings, "worker")
    code = run_worker(
        client_from_settings(settings),
        build_handler(queue, settings),
        queue,
        args.max_messages,
        args.max_wait,
    )
    _log.info("worker_exit", queue=queue, code=code)
    sys.exit(code)
