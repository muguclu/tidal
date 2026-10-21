"""Local Entra-like JWKS and token minting for auth tests (T-113).

Usage in a consumer test package::

    from fakes.jwks.plugin import jwks, mint_token  # noqa: F401  (re-export the fixtures)

or ``pytest_plugins = ["fakes.jwks.plugin"]`` in a top-level conftest.
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
