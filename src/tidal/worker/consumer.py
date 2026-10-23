"""Queue consumer core: parse, DLQ, lock renewal, SIGTERM abandon, result sending.

The Azure objects are injected and only used through the narrow protocols below, which are the
subset of ``azure.servicebus.ServiceBusReceiver`` / ``ServiceBusSender`` that the worker needs, so
tests pass a fake. All broker calls happen on the calling thread; the job itself runs on a helper
thread so the caller can renew the message lock and honour SIGTERM while it computes.
"""

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol

from azure.servicebus import ServiceBusMessage
from pydantic import ValidationError

from tidal import __version__
from tidal.common.logging import correlation_scope, get_logger
from tidal.contracts.messages import CONTENT_TYPE, MAX_MESSAGE_BYTES

_log = get_logger(__name__)

PRODUCER: Final = f"tidal-worker@{__version__}"
#: Broker limit on a dead-letter reason / description is 4 KiB; stay below it.
MAX_DLQ_TEXT: Final = 2048
DEFAULT_RENEW_INTERVAL_S: Final = 20.0  # tiering-jobs lock is 1 min in the emulator config
EXIT_SIGTERM: Final = 143


class ReceivedMessage(Protocol):
    """Subset of ``ServiceBusReceivedMessage`` that the worker reads."""

    @property
    def body(self) -> Iterable[bytes | memoryview]:
        """Body sections (azure yields byte chunks)."""
        ...

    @property
    def message_id(self) -> str | None:
        """Idempotency key."""
        ...

    @property
    def correlation_id(self) -> str | None:
        """Correlation id."""
        ...

    @property
    def delivery_count(self) -> int | None:
        """Broker delivery counter (first delivery counts as 1)."""
        ...


# Any in the Receiver protocol: azure returns its own message class; the worker only reads it
# through ReceivedMessage and hands it back untouched for settlement.
class Receiver(Protocol):
    """Subset of ``ServiceBusReceiver`` (queue, peek-lock)."""

    def receive_messages(
        self, max_message_count: int | None = ..., max_wait_time: float | None = ...
    ) -> list[Any]:
        """Receive up to ``max_message_count`` locked messages."""
        ...

    def complete_message(self, message: Any) -> None:
        """Settle as done."""
        ...

    def abandon_message(self, message: Any) -> None:
        """Release the lock so the message is redelivered."""
        ...

    def dead_letter_message(
        self, message: Any, reason: str | None = ..., error_description: str | None = ...
    ) -> None:
        """Move to the dead-letter sub-queue."""
        ...

    def renew_message_lock(self, message: Any) -> object:
        """Extend the lock."""
        ...


class Sender(Protocol):
    """Subset of ``ServiceBusSender``."""

    def send_messages(self, message: ServiceBusMessage) -> None:
        """Send one message."""
        ...


class MessageTooLarge(ValueError):  # noqa: N818 - reads as a condition
    """A serialised message exceeds ``MAX_MESSAGE_BYTES`` (messaging §4)."""


class PermanentFailure(Exception):  # noqa: N818 - reads as a verdict, not an error type
    """The message can never succeed: dead-letter it with ``reason`` (short) and ``description``."""

    def __init__(self, reason: str, description: str = "") -> None:
        """Create the verdict."""
        super().__init__(reason)
        self.reason = reason
        self.description = description


@dataclass(frozen=True)
class Outbound:
    """A message to send to another queue after the job ran."""

    queue: str
    body: bytes
    message_id: str
    correlation_id: str
    schema: str


class Verdict(StrEnum):
    """What happened to one received message."""

    COMPLETED = "completed"
    ABANDONED = "abandoned"
    DEAD_LETTERED = "dead_lettered"
    LOCK_LOST = "lock_lost"


class Handler(Protocol):
    """One job type: validate the envelope, run it, say what to send back."""

    schema: str

    def parse(self, body: bytes) -> object:
        """Validate ``body``; raise ``pydantic.ValidationError`` when invalid."""
        ...

    def execute(self, message: object, attempt: int, correlation_id: str) -> list[Outbound]:
        """Run the job (helper thread, no broker calls). Raise to fail; see :class:`Worker`."""
        ...

    def on_exhausted(
        self, message: object, attempt: int, correlation_id: str, error: BaseException
    ) -> list[Outbound]:
        """Messages to send when the last delivery failed (e.g. a ``failed`` result)."""
        ...


def serialise(model: Any) -> bytes:  # Any: any pydantic envelope model
    """Envelope JSON with ``exclude_none=True``; raises :class:`MessageTooLarge` over the cap."""
    data = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    # exclude_none also drops *required* nullable metrics (e.g. hit_rate); put them back as null
    # so the message still validates against BacktestMetrics.
    metrics = data.get("payload", {}).get("metrics")
    if metrics is not None:
        full = model.payload.metrics.model_dump(mode="json")
        data["payload"]["metrics"] = {k: metrics.get(k, full[k]) for k in full}
    body = json.dumps(data, separators=(",", ":")).encode()
    if len(body) > MAX_MESSAGE_BYTES:
        msg = f"serialised message is {len(body)} bytes, limit {MAX_MESSAGE_BYTES}"
        raise MessageTooLarge(msg)
    return body


def to_sb_message(out: Outbound) -> ServiceBusMessage:
    """Build the Service Bus message with the envelope properties (messaging §2)."""
    return ServiceBusMessage(
        out.body,
        content_type=CONTENT_TYPE,
        message_id=out.message_id,
        correlation_id=out.correlation_id,
        application_properties={"schema": out.schema},
    )


def _short(text: str) -> str:
    return text[:MAX_DLQ_TEXT]


def validation_summary(exc: ValidationError) -> str:
    """Error locations and messages only: never echoes input values (could be sensitive)."""
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
    return _short("; ".join(parts))


def _default_wait(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)


@dataclass
class Worker:
    """Processes messages from one queue with one :class:`Handler`."""

    receiver: Receiver
    handler: Handler
    senders: dict[str, Sender]
    max_delivery: int = 3
    renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S
    stop: threading.Event = field(default_factory=threading.Event)
    #: ``wait(event, timeout)``: True when the event was set; tests inject a non-sleeping version.
    wait: Callable[[threading.Event, float], bool] = _default_wait

    #: Set by job completion and by ``request_stop``; the renew loop sleeps on it.
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)

    def request_stop(self) -> None:
        """SIGTERM hook: abandon in-flight work and stop receiving."""
        self.stop.set()
        self._wake.set()

    # -- one message ---------------------------------------------------------------------------
    def process(self, msg: ReceivedMessage) -> Verdict:
        """Handle one locked message and settle it. Never raises for message-level problems."""
        body = b"".join(bytes(part) for part in msg.body)
        corr = msg.correlation_id or msg.message_id or "unknown"
        attempt = max(1, msg.delivery_count or 1)
        with correlation_scope(corr):
            if len(body) > MAX_MESSAGE_BYTES:
                return self._dead_letter(msg, "message-too-large", f"{len(body)} bytes")
            try:
                head = json.loads(body)
            except ValueError:
                return self._dead_letter(msg, "invalid-json", "body is not valid JSON")
            schema = head.get("schema") if isinstance(head, dict) else None
            if schema != self.handler.schema:
                return self._dead_letter(
                    msg, "unknown-schema", _short(f"expected {self.handler.schema!r}")
                )
            try:
                parsed = self.handler.parse(body)
            except ValidationError as exc:
                return self._dead_letter(msg, "invalid-message", validation_summary(exc))
            return self._run(msg, parsed, attempt, corr)

    def _run(self, msg: ReceivedMessage, parsed: object, attempt: int, corr: str) -> Verdict:
        box: dict[str, Any] = {}  # Any: "out" -> list[Outbound], "err" -> BaseException
        done = threading.Event()

        def job() -> None:
            try:
                box["out"] = self.handler.execute(parsed, attempt, corr)
            except BaseException as exc:
                box["err"] = exc
            finally:
                done.set()
                self._wake.set()

        threading.Thread(target=job, name="tidal-job", daemon=True).start()
        while True:
            self._wake.clear()
            if done.is_set():
                break
            timed_out = not self.wait(self._wake, self.renew_interval_s)
            if done.is_set():
                break
            if self.stop.is_set():
                _log.warning("sigterm_abandon", attempt=attempt)
                return self._settle(self.receiver.abandon_message, msg, Verdict.ABANDONED)
            if not timed_out:
                continue
            try:
                self.receiver.renew_message_lock(msg)
            except Exception:
                _log.exception("lock_renew_failed")
                return Verdict.LOCK_LOST
        err: BaseException | None = box.get("err")
        if err is None:
            return self._send_and_complete(msg, box["out"])
        if isinstance(err, PermanentFailure):
            return self._dead_letter(msg, err.reason, err.description)
        if attempt >= self.max_delivery:
            _log.error("max_delivery_exceeded", attempt=attempt, error=type(err).__name__)
            try:
                self._send(self.handler.on_exhausted(parsed, attempt, corr, err))
            except Exception:
                _log.exception("on_exhausted_failed")
            return self._dead_letter(msg, "max-delivery-exceeded", type(err).__name__)
        _log.error("job_failed_retry", attempt=attempt, error=type(err).__name__)
        return self._settle(self.receiver.abandon_message, msg, Verdict.ABANDONED)

    # -- settlement ----------------------------------------------------------------------------
    def _send(self, outs: list[Outbound]) -> None:
        for out in outs:
            self.senders[out.queue].send_messages(to_sb_message(out))

    def _send_and_complete(self, msg: ReceivedMessage, outs: list[Outbound]) -> Verdict:
        try:
            self._send(outs)
        except Exception:
            _log.exception("result_send_failed")
            return self._settle(self.receiver.abandon_message, msg, Verdict.ABANDONED)
        return self._settle(self.receiver.complete_message, msg, Verdict.COMPLETED)

    def _dead_letter(self, msg: ReceivedMessage, reason: str, description: str) -> Verdict:
        _log.warning("dead_letter", reason=reason)
        try:
            self.receiver.dead_letter_message(
                msg, reason=_short(reason), error_description=_short(description)
            )
        except Exception:
            _log.exception("settle_failed")
            return Verdict.LOCK_LOST
        return Verdict.DEAD_LETTERED

    @staticmethod
    def _settle(action: Callable[[Any], None], msg: ReceivedMessage, ok: Verdict) -> Verdict:
        try:
            action(msg)
        except Exception:
            _log.exception("settle_failed")
            return Verdict.LOCK_LOST
        return ok

    # -- loop ----------------------------------------------------------------------------------
    def run(self, max_messages: int = 1, max_wait_s: float = 30.0) -> int:
        """Receive and process until ``max_messages`` (0 = unlimited), idle or SIGTERM.

        Returns the process exit code: 143 after SIGTERM, else 0.
        """
        handled = 0
        while not self.stop.is_set() and (max_messages == 0 or handled < max_messages):
            batch = self.receiver.receive_messages(max_message_count=1, max_wait_time=max_wait_s)
            if not batch:
                if max_messages:
                    break  # ScaledJob style: nothing (left) to do
                continue
            for msg in batch:
                self.process(msg)
                handled += 1
        return EXIT_SIGTERM if self.stop.is_set() else 0
