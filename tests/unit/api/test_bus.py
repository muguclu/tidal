"""``ServiceBusBus`` against a fake async client: grouping, batching, lifecycle, settings."""

import asyncio
from typing import Any

import pytest
from azure.servicebus import ServiceBusMessage
from azure.servicebus.exceptions import MessageSizeExceededError
from pydantic import SecretStr

from tidal.api.app import create_app
from tidal.api.bus import (
    BusSendError,
    ServiceBusBus,
    bus_from_settings,
    queue_for,
    to_service_bus_message,
)
from tidal.common.config import ServiceBusSettings, Settings
from tidal.common.errors import ConfigError
from tidal.contracts.messages import (
    QUEUE_BACKTEST_JOBS,
    QUEUE_TIERING_JOBS,
    BacktestJobMessage,
    TieringJobMessage,
)
from tidal.query.protocols import Bus, OutboundMessage
from tidal.worker.handlers import BacktestHandler, TieringHandler

pytestmark = pytest.mark.anyio

JOB = "bt_01HZX3Y7K9QWE5TY8VPAS3DFGH"
NOW = "2026-10-21T10:00:00Z"
CONN = (
    "Endpoint=sb://localhost;SharedAccessKeyName=k;SharedAccessKey=S;UseDevelopmentEmulator=true;"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeBatch:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.messages: list[ServiceBusMessage] = []

    def add_message(self, message: ServiceBusMessage) -> None:
        if len(self.messages) >= self.capacity:
            raise MessageSizeExceededError()
        self.messages.append(message)


class FakeSender:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.sent: list[FakeBatch] = []
        self.opened = 0
        self.exited = 0
        self.active = 0
        self.max_active = 0
        self.closed = False

    async def __aenter__(self) -> "FakeSender":
        self.opened += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1

    async def create_message_batch(self) -> FakeBatch:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)  # yield: an unlocked second caller would interleave here
        return FakeBatch(self.capacity)

    async def send_messages(self, batch: FakeBatch) -> None:
        self.sent.append(batch)
        self.active -= 1

    async def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, capacity: int = 100) -> None:
        self.capacity = capacity
        self.senders: dict[str, FakeSender] = {}
        self.closed = False

    def get_queue_sender(self, queue: str) -> FakeSender:
        return self.senders.setdefault(queue, FakeSender(self.capacity))

    async def close(self) -> None:
        self.closed = True


def tiering(i: int) -> TieringJobMessage:
    return TieringJobMessage.model_validate(
        {
            "schema": "tiering.job/v1",
            "message_id": f"tr_{i}:md.bar_hot",
            "correlation_id": "corr-1",
            "produced_at": NOW,
            "producer": "tidal-api@1.0.0",
            "payload": {
                "table": "md.bar_hot",
                "hot_window_days": 90,
                "dry_run": False,
                "batch_rows": 1000,
            },
        }
    )


def backtest() -> BacktestJobMessage:
    return BacktestJobMessage.model_validate(
        {
            "schema": "backtest.job/v1",
            "message_id": JOB,
            "correlation_id": "corr-1",
            "produced_at": NOW,
            "producer": "tidal-api@1.0.0",
            "payload": {
                "job_id": JOB,
                "output_prefix": f"backtests/{JOB}/",
                "request": {
                    "strategy": "ma_crossover",
                    "instrument_ids": ["EIA.WTI.SPOT"],
                    "params": {"fast": 5, "slow": 20},
                    "interval": "1d",
                    "start": "2020-01-01T00:00:00Z",
                    "end": "2021-01-01T00:00:00Z",
                    "costs": {"fee_bps": 1, "slippage_bps": 1},
                    "seed": 1,
                },
            },
        }
    )


async def test_groups_by_queue_and_sets_envelope_properties() -> None:
    client = FakeClient()
    bus: Bus = ServiceBusBus(client)
    msgs: list[OutboundMessage] = [backtest(), tiering(1)]
    assert await bus.send_batch(msgs) == 2
    (job,) = client.senders[QUEUE_BACKTEST_JOBS].sent[0].messages
    (tier,) = client.senders[QUEUE_TIERING_JOBS].sent[0].messages
    assert job.message_id == JOB
    assert job.correlation_id == "corr-1"
    assert job.content_type == "application/json"
    assert job.application_properties is not None
    assert job.application_properties["schema"] == "backtest.job/v1"
    assert tier.application_properties is not None
    assert tier.application_properties["schema"] == "tiering.job/v1"
    assert tier.message_id == "tr_1:md.bar_hot"


async def test_new_batch_when_full_and_count_returned() -> None:
    client = FakeClient(capacity=2)
    bus = ServiceBusBus(client)
    assert await bus.send_batch([tiering(i) for i in range(5)]) == 5
    sizes = [len(b.messages) for b in client.senders[QUEUE_TIERING_JOBS].sent]
    assert sizes == [2, 2, 1]


async def test_single_oversized_message_raises() -> None:
    bus = ServiceBusBus(FakeClient(capacity=0))
    with pytest.raises(BusSendError) as err:
        await bus.send_batch([tiering(1)])
    assert isinstance(err.value.__cause__, MessageSizeExceededError)
    assert err.value.sent == 0


async def test_empty_send_is_zero() -> None:
    client = FakeClient()
    assert await ServiceBusBus(client).send_batch([]) == 0
    assert not client.senders


async def test_sender_cached_ping_sends_nothing_and_aclose() -> None:
    client = FakeClient()
    bus = ServiceBusBus(client)
    await bus.ping()
    await bus.send_batch([backtest()])
    await bus.send_batch([backtest()])
    sender = client.senders[QUEUE_BACKTEST_JOBS]
    assert len(client.senders) == 1
    assert sender.opened == 1  # ping's own short-lived link, closed again
    assert sender.exited == 1
    assert len(sender.sent) == 2
    await bus.aclose()
    assert sender.closed
    assert client.closed


def settings(**env: str) -> Settings:
    return Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true", **env})


def dev_settings(**env: str) -> Settings:
    return Settings.from_env(
        {
            "TIDAL_ENV": "dev",
            "TIDAL_AUTH__TENANT_ID": "11111111-1111-1111-1111-111111111111",
            "TIDAL_SQL__AUTH": "entra_mi",
            "TIDAL_SQL__SERVER": "x.database.windows.net",
            "TIDAL_CURSOR__KEYVAULT_URL": "https://kv-test.vault.azure.net",
            "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
            **env,
        }
    )


async def test_settings_local_connection_string() -> None:
    bus = bus_from_settings(settings(TIDAL_SERVICEBUS__CONNECTION_STRING=CONN))
    assert isinstance(bus, ServiceBusBus)
    await bus.aclose()


def test_settings_local_without_connection_string_is_none() -> None:
    assert bus_from_settings(settings()) is None


def test_settings_fqdn_uses_managed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Stub:
        def __init__(self, fqdn: str, credential: object, **kw: object) -> None:
            seen.update(fqdn=fqdn, credential=credential)

    cred = object()
    monkeypatch.setattr("azure.servicebus.aio.ServiceBusClient", Stub)
    monkeypatch.setattr("tidal.api.auth.build_credential", lambda **kw: cred)
    bus = bus_from_settings(dev_settings(TIDAL_SERVICEBUS__FQDN="sb-tidal.servicebus.windows.net"))
    assert isinstance(bus, ServiceBusBus)
    assert seen == {"fqdn": "sb-tidal.servicebus.windows.net", "credential": cred}
    assert bus._credential is cred


def test_connection_string_off_local_is_config_error() -> None:
    # Settings validation already forbids this; the factory re-checks (defence in depth).
    s = dev_settings(TIDAL_SERVICEBUS__FQDN="sb-tidal.servicebus.windows.net")
    s = s.model_copy(
        update={
            "servicebus": ServiceBusSettings(
                connection_string=SecretStr(CONN), fqdn=s.servicebus.fqdn
            )
        }
    )
    with pytest.raises(ConfigError):
        bus_from_settings(s)


@pytest.mark.parametrize("fqdn", ["localhost", "", "example.com"])
def test_non_namespace_fqdn_off_local_is_config_error(fqdn: str) -> None:
    with pytest.raises(ConfigError):
        bus_from_settings(dev_settings(TIDAL_SERVICEBUS__FQDN=fqdn))


async def test_concurrent_send_batches_do_not_overlap() -> None:
    client = FakeClient()
    bus = ServiceBusBus(client)
    results = await asyncio.gather(bus.send_batch([tiering(1)]), bus.send_batch([tiering(2)]))
    assert list(results) == [1, 1]
    assert client.senders[QUEUE_TIERING_JOBS].max_active == 1


async def test_ping_concurrent_with_send_is_serialised() -> None:
    client = FakeClient()
    bus = ServiceBusBus(client)
    await asyncio.gather(bus.ping(), bus.send_batch([backtest()]), bus.ping())
    assert client.senders[QUEUE_BACKTEST_JOBS].max_active == 1


async def test_failure_raises_typed_error_with_sent_count() -> None:
    class Failing(FakeSender):
        async def send_messages(self, batch: FakeBatch) -> None:
            if self.sent:
                raise ConnectionError("down")
            await super().send_messages(batch)

    client = FakeClient(capacity=2)
    client.senders[QUEUE_TIERING_JOBS] = Failing(2)
    with pytest.raises(BusSendError) as err:
        await ServiceBusBus(client).send_batch([tiering(i) for i in range(5)])
    assert err.value.sent == 2
    assert err.value.status == 503


async def test_aclose_closes_everything_even_if_a_sender_fails() -> None:
    class Cred:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class Boom(FakeSender):
        async def close(self) -> None:
            raise RuntimeError("boom")

    client, cred = FakeClient(), Cred()
    client.senders[QUEUE_TIERING_JOBS] = Boom(1)
    bus = ServiceBusBus(client, cred)
    await bus.send_batch([tiering(1)])
    with pytest.raises(RuntimeError):
        await bus.aclose()
    assert client.closed
    assert cred.closed


def test_queue_for_unknown_type_raises() -> None:
    with pytest.raises(AssertionError):
        queue_for(object())  # type: ignore[arg-type]  # deliberately wrong type


def test_traceparent_present_when_span_active() -> None:
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("producer"):
        msg = to_service_bus_message(tiering(1))
    assert msg.application_properties is not None
    assert str(msg.application_properties["traceparent"]).startswith("00-")


def test_round_trip_parses_in_worker_handlers() -> None:
    backtest_body = b"".join(bytes(p) for p in to_service_bus_message(backtest()).body)
    tiering_body = b"".join(bytes(p) for p in to_service_bus_message(tiering(1)).body)
    assert BacktestHandler.parse(None, backtest_body) == backtest()  # type: ignore[arg-type]
    assert TieringHandler.parse(None, tiering_body) == tiering(1)  # type: ignore[arg-type]
    for msg, handler in ((backtest(), BacktestHandler), (tiering(1), TieringHandler)):
        props = to_service_bus_message(msg).application_properties or {}
        assert props["schema"] == handler.schema


async def test_lifespan_closes_bus_and_reader_independently() -> None:
    closed: list[str] = []

    class Closer:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name, self.fail = name, fail

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise RuntimeError(self.name)

    app = create_app(
        settings(),
        reader=Closer("reader", fail=True),  # type: ignore[arg-type]
        bus=Closer("bus"),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert sorted(closed) == ["bus", "reader"]
