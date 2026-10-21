"""Local Entra-like JWKS and token minting for auth tests (T-113).

The ``jwks`` and ``mint_token`` fixtures are available everywhere (registered globally in
``tests/conftest.py``); import helpers from ``fakes.jwks``.
"""

from fakes.jwks.core import (
    DEFAULT_AUDIENCE,
    DEFAULT_OID,
    TEST_TENANT_ID,
    JwksFake,
    KeyPair,
    issuer_for,
    jwks_url,
    mint_token,
)

__all__ = [
    "DEFAULT_AUDIENCE",
    "DEFAULT_OID",
    "TEST_TENANT_ID",
    "JwksFake",
    "KeyPair",
    "issuer_for",
    "jwks_url",
    "mint_token",
]
