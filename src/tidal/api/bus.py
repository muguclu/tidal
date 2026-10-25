"""Async Service Bus producer behind the ``Bus`` protocol (SPEC-04, messaging-contract §2, §4).

Messages are routed by envelope type (``backtest.job/v1`` -> ``backtest-jobs``, ``tiering.job/v1``
-> ``tiering-jobs``), serialised with the shared size-capped serialiser, and sent in Service Bus
batches (a new batch starts when the current one is full). Senders are cached per queue.
"""

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any, assert_never

from azure.servicebus import ServiceBusMessage
from azure.servicebus.exceptions import MessageSizeExceededError

from tidal.common.config import Settings
from tidal.common.errors import ConfigError, DependencyUnavailable
from tidal.common.logging import get_correlation_id, get_logger
from tidal.common.telemetry import inject_traceparent
from tidal.contracts.messages import (
    CONTENT_TYPE,
    QUEUE_BACKTEST_JOBS,
    QUEUE_TIERING_JOBS,
    BacktestJobMessage,
    TieringJobMessage,
)
from tidal.query.protocols import Bus, OutboundMessage
from tidal.worker.consumer import serialise

logger = get_logger(__name__)


class BusSendError(DependencyUnavailable):
    """``send_batch`` failed part-way; ``sent`` messages were already delivered."""

    def __init__(self, message: str, *, sent: int) -> None:
        """Carry the number of messages delivered before the failure."""
        super().__init__(message)
        self.sent = sent


def queue_for(message: OutboundMessage) -> str:
    """Destination queue of an outbound envelope; unknown types raise (never a silent default)."""
    if isinstance(message, BacktestJobMessage):
        return QUEUE_BACKTEST_JOBS
    if isinstance(message, TieringJobMessage):
        return QUEUE_TIERING_JOBS
    assert_never(message)


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
    """``Bus`` over an async ``ServiceBusClient`` (injected: tests pass a fake).

    Azure senders are not coroutine-safe, so every use of a queue's sender holds that queue's lock.
    """

    def __init__(
        self,
        client: Any,  # Any: azure.servicebus.aio.ServiceBusClient
        credential: Any = None,  # Any: azure aio credential owned by the bus, closed in aclose
    ) -> None:
        """Wrap ``client``; the bus owns it (and ``credential``) and closes them in aclose."""
        self._client = client
        self._credential = credential
        self._senders: dict[str, Any] = {}  # Any: azure async senders
        self._locks: dict[str, asyncio.Lock] = {}

    def _sender(self, queue: str) -> Any:  # Any: azure async sender
        sender = self._senders.get(queue)
        if sender is None:
            sender = self._senders[queue] = self._client.get_queue_sender(queue)
        return sender

    def _lock(self, queue: str) -> asyncio.Lock:
        return self._locks.setdefault(queue, asyncio.Lock())

    async def send_batch(self, messages: Sequence[OutboundMessage]) -> int:
        """Send ``messages`` grouped by queue in size-bounded batches; returns the count sent.

        All-or-raise: on any failure a :class:`BusSendError` (HTTP 503) is raised whose ``sent``
        attribute is the number of messages already delivered (earlier batches stay delivered).
        """
        by_queue: dict[str, list[ServiceBusMessage]] = defaultdict(list)
        for message in messages:
            by_queue[queue_for(message)].append(to_service_bus_message(message))
        sent = 0
        for queue, items in by_queue.items():
            async with self._lock(queue):
                sent += await self._send_queue(queue, items, sent)
            logger.info(
                "bus_sent", queue=queue, count=len(items), correlation_id=get_correlation_id()
            )
        return sent

    async def _send_queue(
        self, queue: str, items: list[ServiceBusMessage], sent_before: int
    ) -> int:
        """Send ``items`` to ``queue`` (lock held); on failure report the true progress."""
        sender = self._sender(queue)
        sent = 0
        try:
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
        except Exception as exc:
            msg = "The message bus rejected the batch."
            raise BusSendError(msg, sent=sent_before + sent) from exc
        return sent

    async def ping(self) -> None:
        """Open and close a short-lived ``backtest-jobs`` sender link; no message is sent.

        A fresh link each time, so the probe reflects current reachability (a cached open sender
        would stay green after the first success).
        """
        async with (
            self._lock(QUEUE_BACKTEST_JOBS),
            self._client.get_queue_sender(QUEUE_BACKTEST_JOBS),
        ):
            pass

    async def aclose(self) -> None:
        """Close all senders, the client and the credential; one failing does not skip the rest."""
        senders, self._senders = list(self._senders.values()), {}
        async with AsyncExitStack() as stack:
            if self._credential is not None:
                stack.push_async_callback(self._credential.close)
            stack.push_async_callback(self._client.close)
            for sender in senders:
                stack.push_async_callback(sender.close)


def bus_from_settings(settings: Settings) -> Bus | None:
    """Real producer, or ``None`` when Service Bus is not configured (local without a string).

    The connection string is for the local emulator only and is rejected elsewhere
    (``ConfigError``). Outside local the FQDN must be a real namespace
    (``*.servicebus.windows.net``) and is used with the workload's Managed Identity over
    AMQP-over-WebSocket (443 only).
    """
    from azure.servicebus import TransportType  # noqa: PLC0415
    from azure.servicebus.aio import ServiceBusClient  # noqa: PLC0415

    sb = settings.servicebus
    if settings.env == "local":
        if sb.connection_string is None:
            return None
        return ServiceBusBus(
            ServiceBusClient.from_connection_string(
                sb.connection_string.get_secret_value(), transport_type=TransportType.Amqp
            )
        )
    if sb.connection_string is not None:
        msg = "TIDAL_SERVICEBUS__CONNECTION_STRING is only allowed when TIDAL_ENV=local"
        raise ConfigError(msg)
    if not sb.fqdn.endswith(".servicebus.windows.net"):
        msg = "TIDAL_SERVICEBUS__FQDN must be a namespace FQDN outside TIDAL_ENV=local"
        raise ConfigError(msg)
    from tidal.api.auth import build_credential  # noqa: PLC0415

    credential = build_credential(aio=True)
    client = ServiceBusClient(sb.fqdn, credential, transport_type=TransportType.AmqpOverWebsocket)
    return ServiceBusBus(client, credential)
