"""``ServiceBusBus`` against a fake async client: grouping, batching, lifecycle, settings."""

from typing import Any

import pytest
from azure.servicebus import ServiceBusMessage
from azure.servicebus.exceptions import MessageSizeExceededError

from tidal.api.bus import ServiceBusBus, bus_from_settings
from tidal.common.config import Settings
from tidal.contracts.messages import (
    QUEUE_BACKTEST_JOBS,
    QUEUE_TIERING_JOBS,
    BacktestJobMessage,
    TieringJobMessage,
)
from tidal.query.protocols import Bus, OutboundMessage

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
        self.closed = False

    async def __aenter__(self) -> "FakeSender":
        self.opened += 1
        return self

    async def create_message_batch(self) -> FakeBatch:
        return FakeBatch(self.capacity)

    async def send_messages(self, batch: FakeBatch) -> None:
        self.sent.append(batch)

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
    with pytest.raises(MessageSizeExceededError):
        await bus.send_batch([tiering(1)])


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
    assert sender.opened == 1
    assert len(sender.sent) == 2
    await bus.aclose()
    assert sender.closed
    assert client.closed


def settings(**env: str) -> Settings:
    return Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true", **env})


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
    s = Settings.from_env(
        {
            "TIDAL_ENV": "dev",
            "TIDAL_SQL__AUTH": "entra_mi",
            "TIDAL_SQL__SERVER": "x.database.windows.net",
            "TIDAL_CURSOR__KEYVAULT_URL": "https://kv-test.vault.azure.net",
            "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
            "TIDAL_AUTH__TENANT_ID": "11111111-1111-1111-1111-111111111111",
            "TIDAL_SERVICEBUS__FQDN": "sb-tidal.servicebus.windows.net",
        }
    )
    assert isinstance(bus_from_settings(s), ServiceBusBus)
    assert seen == {"fqdn": "sb-tidal.servicebus.windows.net", "credential": cred}
