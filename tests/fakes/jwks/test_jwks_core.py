"""Validate the fake JWKS helpers with PyJWT, including each negative case's failure reason."""

import asyncio
from collections.abc import Callable

import jwt
import pytest

from fakes.jwks.core import DEFAULT_AUDIENCE, JwksFake, issuer_for, jwks_url


def _decode(fake: JwksFake, token: str) -> dict[str, object]:
    # Resolve keys from our in-memory JWKS instead of the network.
    jwk_set = jwt.PyJWKSet.from_dict(fake.jwks())
    kid = jwt.get_unverified_header(token).get("kid")
    matches = [k for k in jwk_set.keys if k.key_id == kid]
    if not matches:
        raise jwt.PyJWKClientError(f"unknown kid {kid}")
    return jwt.decode(
        token,
        matches[0].key,
        algorithms=["RS256"],
        audience=DEFAULT_AUDIENCE,
        issuer=fake.issuer,
        options={"require": ["exp", "nbf", "iss", "aud"]},
    )


def test_valid_token_roundtrip(jwks: JwksFake, mint_token: Callable[..., str]) -> None:
    claims = _decode(jwks, mint_token(roles=["Tidal.Quant"]))
    assert claims["roles"] == ["Tidal.Quant"]
    assert claims["iss"] == issuer_for()


def test_retired_key_still_validates(jwks: JwksFake) -> None:
    token = jwks.mint_token(key=jwks.retired, roles=["Tidal.Reader"])
    assert _decode(jwks, token)["roles"] == ["Tidal.Reader"]


def test_retired_key_removed_after_rotation(jwks: JwksFake) -> None:
    assert len(jwks.jwks()["keys"]) == 2
    assert [k["kid"] for k in jwks.jwks(include_retired=False)["keys"]] == [jwks.active.kid]


def test_expired(jwks: JwksFake) -> None:
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(jwks, jwks.expired_token(roles=["Tidal.Reader"]))


def test_not_yet_valid(jwks: JwksFake) -> None:
    with pytest.raises(jwt.ImmatureSignatureError):
        _decode(jwks, jwks.not_yet_valid_token(roles=["Tidal.Reader"]))


def test_wrong_audience(jwks: JwksFake) -> None:
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(jwks, jwks.wrong_audience_token(roles=["Tidal.Reader"]))


def test_wrong_issuer(jwks: JwksFake) -> None:
    with pytest.raises(jwt.InvalidIssuerError):
        _decode(jwks, jwks.wrong_issuer_token(roles=["Tidal.Reader"]))


def test_unknown_kid(jwks: JwksFake) -> None:
    with pytest.raises(jwt.PyJWKClientError, match="unknown kid"):
        _decode(jwks, jwks.unknown_kid_token(roles=["Tidal.Reader"]))


def test_wrong_kid_header_fails_signature(jwks: JwksFake) -> None:
    token = jwks.mint_token(kid=jwks.retired.kid, roles=["Tidal.Reader"])
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(jwks, token)


def test_alg_none_rejected(jwks: JwksFake) -> None:
    token = jwks.alg_none_token(roles=["Tidal.Admin"])
    assert jwt.get_unverified_header(token)["alg"] == "none"
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(jwks, token)


def test_hs256_confusion_rejected(jwks: JwksFake) -> None:
    token = jwks.hs256_confusion_token(roles=["Tidal.Admin"])
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(jwks, token)


@pytest.mark.parametrize("variant", ["hs256_confusion_der_token", "hs256_confusion_jwk_token"])
def test_hs256_confusion_other_secret_forms_rejected(jwks: JwksFake, variant: str) -> None:
    token = getattr(jwks, variant)(roles=["Tidal.Admin"])
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(jwks, token)


def test_tid_matches_issuer_tenant(jwks: JwksFake) -> None:
    ok = jwt.decode(jwks.mint_token(), options={"verify_signature": False})
    assert ok["tid"] in ok["iss"]
    bad = jwt.decode(jwks.wrong_issuer_token(), options={"verify_signature": False})
    assert bad["tid"] in bad["iss"]
    assert bad["tid"] != jwks.tenant_id


def test_roles_str_rejected(jwks: JwksFake) -> None:
    with pytest.raises(TypeError):
        jwks.mint_token(roles="Tidal.Quant")


def test_missing_roles_claim(jwks: JwksFake) -> None:
    claims = _decode(jwks, jwks.missing_roles_token())
    assert "roles" not in claims


def test_unsupported_alg(jwks: JwksFake) -> None:
    with pytest.raises(ValueError, match="unsupported alg"):
        jwks.mint_token(alg="ES999")


def test_transport_serves_jwks_without_network(jwks: JwksFake) -> None:
    with jwks.client() as client:
        ok = client.get(jwks_url())
        missing = client.get("https://example.invalid/other")
    assert ok.status_code == 200
    assert ok.json() == jwks.jwks()
    assert missing.status_code == 404


def test_async_client(jwks: JwksFake) -> None:
    async def fetch() -> dict[str, list[dict[str, str]]]:
        async with jwks.async_client() as client:
            resp = await client.get(jwks.url)
        body: dict[str, list[dict[str, str]]] = resp.json()
        return body

    body = asyncio.run(fetch())
    assert body["keys"][0]["kid"] == jwks.active.kid


def test_no_private_material_in_jwks(jwks: JwksFake) -> None:
    for jwk in jwks.jwks()["keys"]:
        assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}
