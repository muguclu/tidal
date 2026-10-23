"""Opaque, HMAC-signed, principal- and query-bound cursors for ``format=json`` pagination.

A cursor encodes the position of the first un-emitted row: ``(ts_ns, idx)`` where ``idx`` is the
ordinal of the row among rows sharing that ``ts_ns`` (ticks can collide). The signing key is a
per-process random key held on ``app.state``.

LIMITATION (until T-402 adds a configured key): there is no key in settings yet, so cursors are
valid only on the process that issued them (and die on restart). Multi-instance deployments need
a shared key.

Pages are read from the reader's stream starting at the cursor position (see
``tidal.api.streaming.page_from_stream``), so a page costs at most one reader slice.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass

from fastapi import Request

from tidal.common.errors import ValidationFailed

DEFAULT_LIMIT = 10_000
MAX_LIMIT = 100_000
_BAD = "Invalid cursor."
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,512}")  # base64url + the dot separator


@dataclass(frozen=True, slots=True)
class Position:
    """Position of the first row of the next page."""

    ts_ns: int
    idx: int
    as_of_ns: int | None = None  # pinned knowledge time (bars only), see the bars router
    anchor_ns: int | None = None  # ffill: last real bar before this position (read start)


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
    payload = _b64(
        json.dumps(
            [pos.ts_ns, pos.idx, bound, pos.as_of_ns, pos.anchor_ns], separators=(",", ":")
        ).encode()
    )
    return f"{payload}.{_sign(key, payload)}"


def decode_cursor(key: bytes, bound: str, token: str) -> Position:
    """Verify signature and binding; any problem is a 400 (never echoing the token)."""
    if not _TOKEN.fullmatch(token):  # before any crypto; also guarantees ASCII for the compare
        raise ValidationFailed(_BAD)
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(sig.encode(), _sign(key, payload).encode()):
        raise ValidationFailed(_BAD)
    try:
        ts_ns, idx, got, as_of, anchor = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (ValueError, binascii.Error):
        raise ValidationFailed(_BAD) from None
    ints = isinstance(ts_ns, int) and isinstance(idx, int)
    opt_ints = all(v is None or isinstance(v, int) for v in (as_of, anchor))
    if not ints or not opt_ints:
        raise ValidationFailed(_BAD)
    if not hmac.compare_digest(str(got).encode(), bound.encode()):
        raise ValidationFailed(_BAD)
    return Position(ts_ns, idx, as_of, anchor)
