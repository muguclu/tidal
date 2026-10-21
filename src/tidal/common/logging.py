"""Structured JSON logging to stdout (SPEC-10 §1) with correlation ids and secret redaction.

Every line carries ``ts, level, service, version, correlation_id, event``; ``principal_oid`` is
logged only as its sha256 hex digest. Standard-library loggers (uvicorn, Azure SDKs) are routed
through the same processors, so redaction applies to them too.
"""

import hashlib
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

import structlog
from pydantic import SecretStr
from structlog.types import EventDict, Processor, WrappedLogger
from ulid import ULID

from tidal import __version__
from tidal.common.config import Settings

REDACTED = "**********"
SECRET_KEY_PATTERN = re.compile(
    r"password|secret|token|key|connection_string|authorization", re.IGNORECASE
)

# Secret-looking substrings inside free text (rendered messages, exception/traceback text).
_SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(AccountKey|SharedAccessKey|SharedAccessSignature|Password|Pwd|sig)=[^;&\s'\"]+",
            re.IGNORECASE,
        ),
        rf"\1={REDACTED}",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED),
)
PRINCIPAL_KEYS = ("principal_oid", "oid")

_correlation_id: ContextVar[str | None] = ContextVar("tidal_correlation_id", default=None)


def new_correlation_id() -> str:
    """Return a fresh correlation id (a 26-char Crockford base32 ULID)."""
    return str(ULID())


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current context, if any."""
    return _correlation_id.get()


def bind_correlation_id(correlation_id: str | None = None) -> Token[str | None]:
    """Bind ``correlation_id`` (or a new one) to the current context; returns a reset token."""
    return _correlation_id.set(correlation_id or new_correlation_id())


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the correlation id that was bound before ``bind_correlation_id``."""
    _correlation_id.reset(token)


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the ``with`` block and yield it."""
    token = bind_correlation_id(correlation_id)
    try:
        yield _correlation_id.get() or ""
    finally:
        reset_correlation_id(token)


def hash_principal(oid: str) -> str:
    """Return the sha256 hex digest logged in place of an Entra object id."""
    return hashlib.sha256(oid.encode()).hexdigest()


def scrub_text(text: str) -> str:
    """Mask connection-string secrets, SAS signatures, bearer tokens and JWTs inside ``text``."""
    for pattern, replacement in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(key: str, value: object) -> object:
    if isinstance(value, SecretStr) or SECRET_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact_value(key, v) for v in value]
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask secret-like keys and ``SecretStr`` values, and scrub secret patterns in all text.

    Runs after positional-argument and exception formatting, so the rendered ``event`` and the
    ``exception`` traceback text are scrubbed too. Internal ``_``-prefixed keys are left alone.
    """
    return {k: v if k.startswith("_") else _redact_value(k, v) for k, v in event_dict.items()}


def hash_principal_oid(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Replace a logged ``principal_oid`` (or raw ``oid``) with its sha256 hex digest."""
    for key in PRINCIPAL_KEYS:
        oid = event_dict.get(key)
        if oid is not None:
            event_dict[key] = hash_principal(str(oid))
    return event_dict


def add_correlation_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Add the context correlation id unless the event sets one explicitly (null when unbound)."""
    event_dict.setdefault("correlation_id", _correlation_id.get())
    return event_dict


def _static_fields(service: str) -> Processor:
    def add(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("version", __version__)
        return event_dict

    return add


class _TidalHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker type so ``configure_logging`` can replace its own handler (idempotence)."""


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the root logger for JSON lines on stdout; safe to call repeatedly."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _static_fields(settings.service_name),
        add_correlation_id,
        hash_principal_oid,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.format_exc_info,
        redact_secrets,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    handler = _TidalHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(default=str),
            ],
        )
    )
    root = logging.getLogger()
    for old in [h for h in root.handlers if isinstance(h, _TidalHandler)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(settings.log_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger; call ``configure_logging`` once at process start."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
