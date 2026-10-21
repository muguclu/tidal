"""Opaque, HMAC-signed, principal- and query-bound cursors for ``format=json`` pagination.

A cursor encodes the position of the first un-emitted row: ``(ts_ns, idx)`` where ``idx`` is the
ordinal of the row among rows sharing that ``ts_ns`` (ticks can collide). The signing key is a
per-process random key held on ``app.state``.

LIMITATION (until T-402 adds a configured key): there is no key in settings yet, so cursors are
valid only on the process that issued them (and die on restart). Multi-instance deployments need
a shared key. The reader protocol has no offset/limit either, so a page re-reads its range.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from fastapi import Request

from tidal.common.errors import ValidationFailed

DEFAULT_LIMIT = 10_000
MAX_LIMIT = 100_000
_BAD = "Invalid cursor."


@dataclass(frozen=True, slots=True)
class Position:
    """Position of the first row of the next page."""

    ts_ns: int
    idx: int


def cursor_key(request: Request) -> bytes:
    """The per-process signing key (created on first use)."""
    state = request.app.state
    if getattr(state, "cursor_key", None) is None:
        state.cursor_key = secrets.token_bytes(32)
    key: bytes = state.cursor_key
    return key


def binding(*parts: object) -> str:
    """Digest of the principal and the query that a cursor is bound to."""
    return hashlib.sha256("\x00".join(map(str, parts)).encode()).hexdigest()[:32]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def encode_cursor(key: bytes, bound: str, pos: Position) -> str:
    """Serialise and sign ``pos`` for the query identified by ``bound``."""
    payload = _b64(json.dumps([pos.ts_ns, pos.idx, bound], separators=(",", ":")).encode())
    return f"{payload}.{_sign(key, payload)}"


def decode_cursor(key: bytes, bound: str, token: str) -> Position:
    """Verify signature and binding; any problem is a 400 (never echoing the token)."""
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(key, payload)):
        raise ValidationFailed(_BAD)
    try:
        ts_ns, idx, got = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        raise ValidationFailed(_BAD) from None
    if not (isinstance(ts_ns, int) and isinstance(idx, int)) or not hmac.compare_digest(
        str(got), bound
    ):
        raise ValidationFailed(_BAD)
    return Position(ts_ns, idx)


def page_of[T](
    rows: Iterable[T], ts_of: Callable[[T], int], start: Position | None, limit: int
) -> tuple[list[T], Position | None]:
    """Take up to ``limit`` rows at or after ``start``; also return the next position."""
    out: list[T] = []
    prev_ts: int | None = None
    idx = 0
    for row in rows:
        ts = ts_of(row)
        idx = idx + 1 if ts == prev_ts else 0
        prev_ts = ts
        if start is not None and (ts, idx) < (start.ts_ns, start.idx):
            continue
        if len(out) == limit:
            return out, Position(ts, idx)
        out.append(row)
    return out, None
