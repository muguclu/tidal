"""``ServiceBusBus`` against the compose Service Bus emulator (127.0.0.1:5672)."""

import socket

import pytest
from azure.servicebus.aio import ServiceBusClient

from tidal.api.bus import ServiceBusBus, bus_from_settings
from tidal.common.config import Settings
from tidal.contracts.messages import QUEUE_TIERING_JOBS, TieringJobMessage

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONN = (
    "Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;"
    "SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _emulator_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5672), timeout=1):
            return True
    except OSError:
        return False


# Declared skip, not a runtime skip: the CI integration job runs Azurite + SQL only (no Service Bus
# emulator), so TIDAL_TEST_REQUIRE_SQL must not fail it. Locally: `docker compose up -d`.
@pytest.mark.skipif(not _emulator_up(), reason="no Service Bus emulator on 127.0.0.1:5672")
async def test_send_ping_and_receive() -> None:
    settings = Settings.from_env(
        {
            "TIDAL_ENV": "local",
            "TIDAL_AUTH__DISABLED": "true",
            "TIDAL_SERVICEBUS__CONNECTION_STRING": CONN,
        }
    )
    bus = bus_from_settings(settings)
    assert isinstance(bus, ServiceBusBus)
    msg = TieringJobMessage.model_validate(
        {
            "schema": "tiering.job/v1",
            "message_id": "tr_it_api_bus:md.bar_hot",
            "correlation_id": "corr-it",
            "produced_at": "2026-10-21T10:00:00Z",
            "producer": "tidal-api@1.0.0",
            "payload": {
                "table": "md.bar_hot",
                "hot_window_days": 90,
                "dry_run": True,
                "batch_rows": 1000,
            },
        }
    )
    try:
        await bus.ping()
        assert await bus.send_batch([msg]) == 1
    finally:
        await bus.aclose()
    async with (
        ServiceBusClient.from_connection_string(CONN) as client,
        client.get_queue_receiver(QUEUE_TIERING_JOBS) as rx,
    ):
        got = await rx.receive_messages(max_message_count=10, max_wait_time=10)
        ours = [m for m in got if m.message_id == "tr_it_api_bus:md.bar_hot"]
        for m in got:  # touch only our own message; leave everything else on the queue
            if m.message_id == "tr_it_api_bus:md.bar_hot":
                await rx.complete_message(m)
            else:
                await rx.abandon_message(m)
    assert ours
    assert ours[0].correlation_id == "corr-it"
    assert ours[0].content_type == "application/json"
    assert ours[0].application_properties is not None
    assert ours[0].application_properties[b"schema"] == b"tiering.job/v1"
