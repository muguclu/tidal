"""Async Service Bus producer behind the ``Bus`` protocol (SPEC-04, messaging-contract §2, §4).

Messages are routed by envelope type (``backtest.job/v1`` -> ``backtest-jobs``, ``tiering.job/v1``
-> ``tiering-jobs``), serialised with the shared size-capped serialiser, and sent in Service Bus
batches (a new batch starts when the current one is full). Senders are cached per queue.
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from azure.servicebus import ServiceBusMessage
from azure.servicebus.exceptions import MessageSizeExceededError

from tidal.common.config import Settings
from tidal.common.logging import get_correlation_id, get_logger
from tidal.common.telemetry import inject_traceparent
from tidal.contracts.messages import (
    CONTENT_TYPE,
    QUEUE_BACKTEST_JOBS,
    QUEUE_TIERING_JOBS,
    BacktestJobMessage,
)
from tidal.query.protocols import Bus, OutboundMessage
from tidal.worker.consumer import serialise

logger = get_logger(__name__)


def queue_for(message: OutboundMessage) -> str:
    """Destination queue of an outbound envelope."""
    return QUEUE_BACKTEST_JOBS if isinstance(message, BacktestJobMessage) else QUEUE_TIERING_JOBS


def to_service_bus_message(message: OutboundMessage) -> ServiceBusMessage:
    """Envelope -> Service Bus message with ``message_id``/``correlation_id``/``schema`` set."""
    props: dict[str, Any] = {"schema": message.schema_}  # Any: AMQP properties
    inject_traceparent(props)
    amqp_props: dict[str | bytes, Any] = dict(props.items())  # Any: AMQP properties
    return ServiceBusMessage(
        serialise(message),
        content_type=CONTENT_TYPE,
        message_id=message.message_id,
        correlation_id=message.correlation_id,
        application_properties=amqp_props,
    )


class ServiceBusBus:
    """``Bus`` over an async ``ServiceBusClient`` (injected: tests pass a fake)."""

    def __init__(self, client: Any) -> None:  # Any: azure.servicebus.aio.ServiceBusClient
        """Wrap ``client``; the bus owns it and closes it in :meth:`aclose`."""
        self._client = client
        self._senders: dict[str, Any] = {}  # Any: azure async senders

    def _sender(self, queue: str) -> Any:  # Any: azure async sender
        sender = self._senders.get(queue)
        if sender is None:
            sender = self._senders[queue] = self._client.get_queue_sender(queue)
        return sender

    async def send_batch(self, messages: Sequence[OutboundMessage]) -> int:
        """Send ``messages`` grouped by queue in size-bounded batches; returns the count sent."""
        by_queue: dict[str, list[ServiceBusMessage]] = defaultdict(list)
        for message in messages:
            by_queue[queue_for(message)].append(to_service_bus_message(message))
        sent = 0
        for queue, items in by_queue.items():
            sender = self._sender(queue)
            batch = await sender.create_message_batch()
            in_batch = 0
            for item in items:
                try:
                    batch.add_message(item)
                except MessageSizeExceededError:
                    if in_batch == 0:
                        raise  # a single message larger than a whole batch
                    await sender.send_messages(batch)
                    sent += in_batch
                    batch = await sender.create_message_batch()
                    batch.add_message(item)
                    in_batch = 0
                in_batch += 1
            if in_batch:
                await sender.send_messages(batch)
                sent += in_batch
            logger.info(
                "bus_sent", queue=queue, count=len(items), correlation_id=get_correlation_id()
            )
        return sent

    async def ping(self) -> None:
        """Open the ``backtest-jobs`` sender link (no message is sent); raise when unreachable."""
        await self._sender(QUEUE_BACKTEST_JOBS).__aenter__()

    async def aclose(self) -> None:
        """Close all senders and the client."""
        senders, self._senders = list(self._senders.values()), {}
        for sender in senders:
            await sender.close()
        await self._client.close()


def bus_from_settings(settings: Settings) -> Bus | None:
    """Real producer, or ``None`` when Service Bus is not configured.

    A connection string (local emulator) wins; otherwise outside local the FQDN is used with the
    workload's Managed Identity. Off-local the transport is AMQP over WebSocket (443 only).
    """
    from azure.servicebus import TransportType  # noqa: PLC0415
    from azure.servicebus.aio import ServiceBusClient  # noqa: PLC0415

    sb = settings.servicebus
    local = settings.env == "local"
    transport = TransportType.Amqp if local else TransportType.AmqpOverWebsocket
    if sb.connection_string is not None:
        client = ServiceBusClient.from_connection_string(
            sb.connection_string.get_secret_value(), transport_type=transport
        )
        return ServiceBusBus(client)
    if local or not sb.fqdn:
        return None
    from tidal.api.auth import build_credential  # noqa: PLC0415

    return ServiceBusBus(
        ServiceBusClient(sb.fqdn, build_credential(aio=True), transport_type=transport)
    )
