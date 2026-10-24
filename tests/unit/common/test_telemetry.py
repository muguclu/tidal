"""OpenTelemetry setup, distributed trace across api -> query and custom metrics (T-505)."""

import asyncio
import contextvars
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import azure.monitor.opentelemetry as distro
import httpx
import pytest
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from fakes.reader import FakeReader
from tidal.api.app import create_app
from tidal.common import telemetry as tel
from tidal.common.config import Settings
from tidal.contracts.messages import QUEUE_BACKTEST_JOBS
from tidal.query.service import FailClosedValidator, create_query_app, remote_reader_from_settings
from tidal.worker.consumer import Verdict
from tidal.worker.main import TracedWorker

pytestmark = pytest.mark.anyio
LOCAL = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
WTI = "EIA.WTI.SPOT"
Q = {
    "instrument_id": WTI, "interval": "1d",
    "start": "2020-04-01T00:00:00Z", "end": "2020-05-01T00:00:00Z",
}  # fmt: skip


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def otel() -> Iterator[tuple[InMemorySpanExporter, InMemoryMetricReader]]:
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    assert tel.configure_telemetry(
        LOCAL, "api", span_exporter=spans, metric_reader=reader, sampling_ratio=1.0
    )
    yield spans, reader
    tel.shutdown_telemetry()


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    return {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}


def test_noop_when_local_or_without_connection_string() -> None:
    assert not tel.configure_telemetry(LOCAL, "api")
    dev = Settings.model_construct(env="dev", otel=LOCAL.otel)
    assert not tel.telemetry_enabled(dev)
    local_with_cs = LOCAL.model_copy(
        update={"otel": LOCAL.otel.model_copy(update={"connection_string": SecretStr("x")})}
    )
    assert not tel.telemetry_enabled(local_with_cs)
    tel.record_query("bars", "hot", 1)  # no provider: must not raise


def test_azure_monitor_called_once_with_sampling_and_secret_not_logged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()
    monkeypatch.setattr("tidal.api.auth.build_credential", lambda **_: sentinel)
    monkeypatch.setattr(distro, "configure_azure_monitor", lambda **kw: calls.append(kw))
    secret = "InstrumentationKey=00000000-0000-0000-0000-000000000000"  # noqa: S105
    dev = LOCAL.model_copy(
        update={
            "env": "dev",
            "otel": LOCAL.otel.model_copy(update={"connection_string": SecretStr(secret)}),
        }
    )
    try:
        assert tel.configure_telemetry(dev, "api")
        assert tel.configure_telemetry(dev, "api")  # idempotent
    finally:
        tel.shutdown_telemetry()
    assert len(calls) == 1
    assert calls[0]["sampling_ratio"] == 0.2
    assert calls[0]["credential"] is sentinel
    assert secret not in repr(dev) + capsys.readouterr().out


def test_message_span_links_to_producer_traceparent(
    otel: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    spans, _ = otel
    with tel.get_tracer().start_as_current_span("submit") as producer:
        props: dict[str, Any] = {}
        tel.inject_traceparent(props)
        pid = producer.get_span_context().trace_id
    # AMQP application properties come back as bytes keys/values
    amqp = {k.encode(): v.encode() for k, v in props.items()}
    with tel.message_span("backtest-jobs process", amqp):
        pass
    consumer = next(s for s in spans.get_finished_spans() if s.name == "backtest-jobs process")
    assert consumer.links[0].context.trace_id == pid
    assert consumer.parent is None


async def test_one_trace_across_api_and_query_and_metrics(
    otel: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    spans, reader = otel
    query = create_query_app(FakeReader(), tel_validator(), LOCAL)
    rr = remote_reader_from_settings(LOCAL, transport=IsolatedASGI(query))
    HTTPXClientInstrumentor.instrument_client(
        rr._client, tracer_provider=tel._state.tracer_provider
    )
    api = create_app(LOCAL, reader=rr)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as c:
        r = await c.get("/v1/bars", params=Q, headers={"X-Correlation-Id": "cid-1"})
        assert r.status_code == 200
        assert r.headers["X-Correlation-Id"] == "cid-1"
        assert (await c.get("/v1/bars", params={**Q, "format": "arrow"})).status_code == 200
    finished = spans.get_finished_spans()
    server = [s for s in finished if s.kind.name == "SERVER"]
    assert len(server) >= 2
    first = [
        s for s in finished if s.attributes and s.attributes.get("tidal.correlation_id") == "cid-1"
    ]
    assert len(first) >= 2, "both apps tag the span with the X-Correlation-Id"
    assert len({s.context.trace_id for s in first}) == 1
    assert any(
        s.kind.name == "CLIENT" for s in finished if s.context.trace_id == first[0].context.trace_id
    )
    names = _metric_names(reader)
    assert {tel.M_QUERY_ROWS, tel.M_QUERY_TIER, tel.M_QUERY_BYTES} <= names


def test_worker_and_other_metrics_emit(
    otel: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, reader = otel
    tel.record_backtest_duration(12.0, "completed")
    tel.record_queue_lag(5.0, "backtest-jobs")
    tel.record_cold_start("worker")
    tel.record_cold_start("worker")
    tel.record_rows_moved(10, "hot_to_cold")
    tel.record_rows_written(10, "synthetic")
    assert {
        tel.M_BACKTEST_DURATION, tel.M_QUEUE_LAG, tel.M_COLD_START,
        tel.M_TIERING_ROWS, tel.M_INGEST_ROWS,
    } <= _metric_names(reader)  # fmt: skip


class IsolatedASGI(httpx.AsyncBaseTransport):
    """Run the query app in an empty context, so only the traceparent header can link traces."""

    def __init__(self, app: Any) -> None:
        self.inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        task = asyncio.get_running_loop().create_task(
            self.inner.handle_async_request(request), context=contextvars.Context()
        )
        return await task


def tel_validator() -> Any:
    return FailClosedValidator()


class _Receiver:
    def __init__(self) -> None:
        self.settled: list[str] = []

    def dead_letter_message(self, message: Any, reason: str | None = None, **_: Any) -> None:
        self.settled.append(reason or "")


class _Msg:
    body = (b'{"schema": "other"}',)
    message_id = "m1"
    correlation_id = "corr-1"
    delivery_count = 0
    enqueued_time_utc = datetime(2020, 1, 1, tzinfo=UTC)

    def __init__(self, props: dict[bytes, bytes]) -> None:
        self.application_properties = props


def test_traced_worker_span_linked_and_metrics(
    otel: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    spans, reader = otel
    with tel.get_tracer().start_as_current_span("submit") as producer:
        props: dict[str, Any] = {}
        tel.inject_traceparent(props)
    amqp = {k.encode(): v.encode() for k, v in props.items()}
    receiver = _Receiver()
    worker = TracedWorker(receiver, SimpleNamespace(schema="mine"), {}, max_delivery=3)  # type: ignore[arg-type]
    worker.queue = QUEUE_BACKTEST_JOBS
    assert worker.process(_Msg(amqp)) is Verdict.DEAD_LETTERED
    span = next(s for s in spans.get_finished_spans() if s.name.endswith("process"))
    assert span.links[0].context.span_id == producer.get_span_context().span_id
    assert {tel.M_BACKTEST_DURATION, tel.M_QUEUE_LAG, tel.M_COLD_START} <= _metric_names(reader)
