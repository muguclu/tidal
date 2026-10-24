"""OpenTelemetry traces and custom metrics (SPEC-10 §1, T-505).

``configure_telemetry(settings, service_name)`` is the single setup call. With
``TIDAL_OTEL__CONNECTION_STRING`` set and ``TIDAL_ENV != local`` it hands over to
``azure-monitor-opentelemetry`` (20 % trace sampling, SPEC-09 C8); otherwise it is a no-op. Tests
inject in-memory exporters, which installs private providers instead of the process-global ones.
The connection string is passed straight to the distro and is never logged or stored here.

Head sampling cannot keep "100 % of errors" by itself; that part of SPEC-10 needs a tail sampler
(collector) and is out of scope for the in-process setup.

Metric attributes are deliberately low-cardinality (tier, endpoint, queue, outcome); never put an
instrument id, principal or correlation id on a metric.
"""

import threading
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Link, Span, SpanKind, Tracer

from tidal.common.config import Settings

SAMPLING_RATIO = 0.2
INSTRUMENTATION_NAME = "tidal"

# Stable metric names (SPEC-10 §1) plus the worker / cold-start extras.
M_QUERY_ROWS = "tidal.query.rows"
M_QUERY_BYTES = "tidal.query.bytes_scanned"
M_QUERY_TIER = "tidal.query.tier"
M_BACKTEST_DURATION = "tidal.backtest.duration_ms"
M_TIERING_ROWS = "tidal.tiering.rows_moved"
M_INGEST_ROWS = "tidal.ingest.rows_written"
M_QUEUE_LAG = "tidal.worker.queue_lag_ms"
M_COLD_START = "tidal.process.cold_start_ms"

_PROCESS_START = time.monotonic()
_lock = threading.Lock()


@dataclass
class _State:
    configured: bool = False
    tracer_provider: Any = None  # SDK TracerProvider (private/test path) or None for the global
    meter_provider: Any = None  # SDK MeterProvider (private/test path) or None for the global
    cold_start_recorded: bool = False
    instruments: dict[str, Any] = field(default_factory=dict)


_state = _State()


def telemetry_enabled(settings: Settings) -> bool:
    """True when the Azure Monitor exporter should run: a connection string and not local."""
    return settings.env != "local" and settings.otel.connection_string is not None


def configure_telemetry(
    settings: Settings,
    service_name: str,
    *,
    span_exporter: Any = None,
    metric_reader: Any = None,
    sampling_ratio: float = SAMPLING_RATIO,
) -> bool:
    """Set up tracing and metrics once per process; returns True when telemetry is active.

    ``span_exporter`` / ``metric_reader`` (tests) build private SDK providers and skip Azure
    Monitor. Calling again after a successful setup is a no-op.
    """
    with _lock:
        if _state.configured:
            return True
        if span_exporter is not None or metric_reader is not None:
            _configure_private(service_name, span_exporter, metric_reader, sampling_ratio)
        elif telemetry_enabled(settings):
            from azure.monitor.opentelemetry import configure_azure_monitor  # noqa: PLC0415
            from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

            from tidal.api.auth import build_credential  # noqa: PLC0415 - lazy: avoids a cycle

            secret = settings.otel.connection_string
            assert secret is not None  # noqa: S101 - telemetry_enabled checked it
            configure_azure_monitor(
                connection_string=secret.get_secret_value(),
                # Entra-only ingestion (ADR-0008 R-24): the connection string alone cannot auth.
                credential=build_credential(),
                resource=Resource.create({"service.name": f"tidal-{service_name}"}),
                sampling_ratio=sampling_ratio,
                enable_live_metrics=False,
            )
        else:
            return False
        _state.configured = True
        _state.instruments = {}
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

    # W3C traceparent goes out on every tidal-api -> tidal-query call; X-Correlation-Id is separate.
    HTTPXClientInstrumentor().instrument(tracer_provider=_state.tracer_provider)
    return True


def _configure_private(
    service_name: str,
    span_exporter: Any,
    metric_reader: Any,
    ratio: float,
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased  # noqa: PLC0415

    resource = Resource.create({"service.name": f"tidal-{service_name}"})
    tp = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(ratio)))
    if span_exporter is not None:
        tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    readers = [metric_reader] if metric_reader is not None else []
    _state.tracer_provider = tp
    _state.meter_provider = MeterProvider(resource=resource, metric_readers=readers)


def instrument_fastapi(app: Any) -> None:
    """Server spans for ``app`` (no-op until telemetry is configured)."""
    if not _state.configured:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_state.tracer_provider, meter_provider=_state.meter_provider
    )


def shutdown_telemetry() -> None:
    """Flush private providers and reset state (tests; also safe at process exit)."""
    with _lock:
        if _state.configured:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

            HTTPXClientInstrumentor().uninstrument()
        if _state.tracer_provider is not None:
            _state.tracer_provider.shutdown()
        if _state.meter_provider is not None:
            _state.meter_provider.shutdown()
        _state.configured = False
        _state.tracer_provider = None
        _state.meter_provider = None
        _state.cold_start_recorded = False
        _state.instruments = {}


# -- tracing helpers ----------------------------------------------------------------------------
def get_tracer() -> Tracer:
    """Tracer from the configured provider, else the (no-op) global one."""
    if _state.tracer_provider is not None:
        tracer: Tracer = _state.tracer_provider.get_tracer(INSTRUMENTATION_NAME)
        return tracer
    return trace.get_tracer(INSTRUMENTATION_NAME)


def current_trace_id() -> str | None:
    """32-hex trace id of the active span, or None. Maps log lines to traces."""
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None


def set_span_correlation_id(correlation_id: str) -> None:
    """Attach the ``X-Correlation-Id`` to the active span so logs and traces join on it."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("tidal.correlation_id", correlation_id)


def inject_traceparent(properties: MutableMapping[str, Any]) -> None:
    """Add W3C ``traceparent`` (and tracestate) to Service Bus application properties."""
    inject(properties)


def _as_str_map(props: Mapping[Any, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (props or {}).items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        out[key] = v.decode() if isinstance(v, bytes) else str(v)
    return out


@contextmanager
def message_span(
    name: str, application_properties: Mapping[Any, Any] | None, **attributes: str | int
) -> Iterator[Span]:
    """Consumer span for one queue message, linked to the producer's ``traceparent`` if present."""
    props = _as_str_map(application_properties)
    producer = trace.get_current_span(extract(props)).get_span_context()
    links = [Link(producer)] if producer.is_valid else []
    token = otel_context.attach(otel_context.Context())  # a fresh trace per message
    try:
        with get_tracer().start_as_current_span(
            name, kind=SpanKind.CONSUMER, links=links, attributes=dict(attributes)
        ) as span:
            yield span
    finally:
        otel_context.detach(token)


# -- metrics ------------------------------------------------------------------------------------
def _instruments() -> dict[str, Any]:
    if not _state.instruments:
        meter = (
            _state.meter_provider.get_meter(INSTRUMENTATION_NAME)
            if _state.meter_provider is not None
            else metrics.get_meter(INSTRUMENTATION_NAME)
        )
        _state.instruments = {
            M_QUERY_ROWS: meter.create_counter(M_QUERY_ROWS, unit="{row}"),
            M_QUERY_BYTES: meter.create_counter(M_QUERY_BYTES, unit="By"),
            M_QUERY_TIER: meter.create_counter(M_QUERY_TIER, unit="{query}"),
            M_BACKTEST_DURATION: meter.create_histogram(M_BACKTEST_DURATION, unit="ms"),
            M_TIERING_ROWS: meter.create_counter(M_TIERING_ROWS, unit="{row}"),
            M_INGEST_ROWS: meter.create_counter(M_INGEST_ROWS, unit="{row}"),
            M_QUEUE_LAG: meter.create_histogram(M_QUEUE_LAG, unit="ms"),
            M_COLD_START: meter.create_histogram(M_COLD_START, unit="ms"),
        }
    return _state.instruments


def record_query(endpoint: str, tier: str, rows: int, bytes_out: int = 0) -> None:
    """Rows (and Arrow bytes when streamed) served by ``tidal-query``, by tier and endpoint."""
    attrs = {"tier": tier, "endpoint": endpoint}
    inst = _instruments()
    inst[M_QUERY_TIER].add(1, attrs)
    inst[M_QUERY_ROWS].add(rows, attrs)
    if bytes_out:
        inst[M_QUERY_BYTES].add(bytes_out, attrs)


def record_backtest_duration(duration_ms: float, outcome: str) -> None:
    """Wall time of one backtest job message; ``outcome`` is a settlement verdict."""
    _instruments()[M_BACKTEST_DURATION].record(duration_ms, {"outcome": outcome})


def record_rows_moved(rows: int, direction: str) -> None:
    """Rows moved by the tiering job (``direction`` e.g. ``hot_to_cold``)."""
    _instruments()[M_TIERING_ROWS].add(rows, {"direction": direction})


def record_rows_written(rows: int, source: str) -> None:
    """Rows written by ingest, by source (eia, fred, synthetic)."""
    _instruments()[M_INGEST_ROWS].add(rows, {"source": source})


def record_queue_lag(lag_ms: float, queue: str) -> None:
    """Time a message waited in the queue before the worker picked it up."""
    _instruments()[M_QUEUE_LAG].record(max(lag_ms, 0.0), {"queue": queue})


def record_cold_start(service: str) -> None:
    """Process start to first unit of work, recorded once per process."""
    if _state.cold_start_recorded:
        return
    _state.cold_start_recorded = True
    elapsed_ms = (time.monotonic() - _PROCESS_START) * 1000
    _instruments()[M_COLD_START].record(elapsed_ms, {"service": service})
