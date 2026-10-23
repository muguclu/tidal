"""``tidal dlq inspect | resubmit``: look at and replay dead-lettered messages (messaging §4).

The Service Bus client is a parameter of the core functions (tests pass a fake); the typer
commands only build the real client.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import typer
from azure.servicebus import ServiceBusMessage, ServiceBusSubQueue

from tidal.common.config import Settings, get_settings
from tidal.contracts.messages import (
    QUEUE_BACKTEST_JOBS,
    QUEUE_BACKTEST_RESULTS,
    QUEUE_TIERING_JOBS,
)

QUEUES = (QUEUE_BACKTEST_JOBS, QUEUE_BACKTEST_RESULTS, QUEUE_TIERING_JOBS)

app = typer.Typer(
    name="dlq",
    help="Inspect and resubmit dead-lettered Service Bus messages.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


class BusClient(Protocol):
    """Subset of ``ServiceBusClient`` used here."""

    def get_queue_receiver(self, queue_name: str, **kwargs: Any) -> Any:  # Any: azure receiver
        """Receiver for a queue or its sub-queue."""
        ...

    def get_queue_sender(self, queue_name: str, **kwargs: Any) -> Any:  # Any: azure sender
        """Sender for a queue."""
        ...


@dataclass(frozen=True)
class DlqEntry:
    """One dead-lettered message, without its body."""

    message_id: str | None
    schema: str | None
    reason: str | None
    description: str | None
    delivery_count: int | None
    body_bytes: int


def _body(msg: Any) -> bytes:  # Any: azure message
    return b"".join(bytes(p) for p in msg.body)


def _schema(msg: Any) -> str | None:  # Any: azure message
    props = msg.application_properties or {}
    for key, value in props.items():
        if (key.decode() if isinstance(key, bytes) else key) == "schema":
            return value.decode() if isinstance(value, bytes) else str(value)
    return None


def inspect(client: BusClient, queue: str, max_count: int = 50) -> list[DlqEntry]:
    """Peek (non-destructively) up to ``max_count`` dead-lettered messages of ``queue``."""
    with client.get_queue_receiver(queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER) as rx:
        msgs = rx.peek_messages(max_message_count=max_count)
        return [
            DlqEntry(
                message_id=m.message_id,
                schema=_schema(m),
                reason=m.dead_letter_reason,
                description=m.dead_letter_error_description,
                delivery_count=m.delivery_count,
                body_bytes=len(_body(m)),
            )
            for m in msgs
        ]


def resubmit(
    client: BusClient,
    queue: str,
    max_count: int = 50,
    message_id: str | None = None,
    max_wait_s: float = 5.0,
) -> int:
    """Move dead-lettered messages back to ``queue`` (copy, then complete); returns the count.

    With ``message_id`` only that message is moved; others are abandoned back to the DLQ.
    Send happens before complete: a crash in between duplicates, never loses; consumers are
    idempotent on redelivery.
    """
    moved = 0
    with (
        client.get_queue_receiver(queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER) as rx,
        client.get_queue_sender(queue) as tx,
    ):
        for m in rx.receive_messages(max_message_count=max_count, max_wait_time=max_wait_s):
            if message_id is not None and m.message_id != message_id:
                rx.abandon_message(m)
                continue
            props = {
                (k.decode() if isinstance(k, bytes) else k): v
                for k, v in (m.application_properties or {}).items()
            }
            tx.send_messages(
                ServiceBusMessage(
                    _body(m),
                    content_type=m.content_type,
                    message_id=m.message_id,
                    correlation_id=m.correlation_id,
                    application_properties=props,
                )
            )
            rx.complete_message(m)
            moved += 1
    return moved


def client_from_settings(settings: Settings) -> BusClient:
    """Real client: connection string (emulator / local) or managed identity on the FQDN.

    Off-local the transport is AMQP over WebSocket (TCP 443): the AKS NetworkPolicy allows only
    443 and DNS egress, so plain AMQP (5671) would be blocked. Local keeps plain AMQP.
    """
    from azure.servicebus import ServiceBusClient, TransportType  # noqa: PLC0415

    transport = TransportType.Amqp if settings.env == "local" else TransportType.AmqpOverWebsocket
    conn = settings.servicebus.connection_string
    if conn is not None:
        return ServiceBusClient.from_connection_string(
            conn.get_secret_value(), transport_type=transport
        )
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415

    return ServiceBusClient(
        settings.servicebus.fqdn, DefaultAzureCredential(), transport_type=transport
    )


def _client() -> BusClient:
    return client_from_settings(get_settings())


@app.command("inspect")
def inspect_cmd(
    queue: Annotated[str, typer.Argument(help=f"One of: {', '.join(QUEUES)}")],
    max_count: Annotated[int, typer.Option("--max", min=1, max=1000)] = 50,
) -> None:
    """List dead-lettered messages (id, schema, reason, delivery count); bodies are not printed."""
    for e in inspect(_client(), queue, max_count):
        typer.echo(
            f"{e.message_id}\t{e.schema}\treason={e.reason}\tdeliveries={e.delivery_count}"
            f"\tbytes={e.body_bytes}\t{e.description or ''}"
        )


@app.command("resubmit")
def resubmit_cmd(
    queue: Annotated[str, typer.Argument(help=f"One of: {', '.join(QUEUES)}")],
    max_count: Annotated[int, typer.Option("--max", min=1, max=1000)] = 50,
    message_id: Annotated[str | None, typer.Option("--message-id")] = None,
) -> None:
    """Send dead-lettered messages back to the main queue and remove them from the DLQ."""
    typer.echo(f"resubmitted {resubmit(_client(), queue, max_count, message_id)}")
