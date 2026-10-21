"""Pytest fixtures: ``jwks`` (a :class:`JwksFake`) and ``mint_token`` (active-key minter)."""

from collections.abc import Callable

import pytest

from fakes.jwks.core import JwksFake


@pytest.fixture(scope="session")
def jwks() -> JwksFake:
    """Session-wide fake tenant keys (generated once; RSA keygen is slow)."""
    return JwksFake.create()


@pytest.fixture
def mint_token(jwks: JwksFake) -> Callable[..., str]:
    """Callable minting tokens signed by the active key (kwargs of ``JwksFake.mint_token``)."""
    return jwks.mint_token
