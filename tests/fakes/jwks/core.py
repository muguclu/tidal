"""RS256 keypairs, JWKS documents and token minting (incl. forged/negative tokens).

No key material is committed: keys are generated at runtime. Forged tokens (``alg=none`` and the
HS256 key-confusion attack) are assembled by hand because PyJWT refuses to produce them.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any  # Any: JWT claims/headers are arbitrary JSON values

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_TENANT_ID = "11111111-2222-3333-4444-555555555555"
DEFAULT_AUDIENCE = "api://tidal"
FOREIGN_TENANT_ID = "99999999-9999-9999-9999-999999999999"
DEFAULT_OID = "00000000-0000-0000-0000-0000000000a1"


def issuer_for(tenant_id: str = TEST_TENANT_ID) -> str:
    """Entra v2.0 issuer string for ``tenant_id``."""
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def jwks_url(tenant_id: str = TEST_TENANT_ID) -> str:
    """Entra v2.0 JWKS discovery URL for ``tenant_id``."""
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


@dataclass(frozen=True)
class KeyPair:
    """An RSA signing key with its ``kid``."""

    kid: str
    private_key: rsa.RSAPrivateKey = field(repr=False)

    @classmethod
    def generate(cls, kid: str) -> "KeyPair":
        """Generate a fresh 2048-bit RSA key."""
        return cls(kid, rsa.generate_private_key(public_exponent=65537, key_size=2048))

    @classmethod
    def from_pem(cls, kid: str, pem: bytes) -> "KeyPair":
        """Load an unencrypted PKCS8 PEM private key."""
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("expected an RSA private key")
        return cls(kid, key)

    def private_pem(self) -> bytes:
        """PKCS8 PEM of the private key (never print this)."""
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def public_pem(self) -> bytes:
        """SubjectPublicKeyInfo PEM of the public key."""
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def public_der(self) -> bytes:
        """DER SubjectPublicKeyInfo of the public key."""
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def public_jwk(self) -> dict[str, str]:
        """Public JWK (RFC 7517) for this key."""
        numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _int_b64(numbers.n),
            "e": _int_b64(numbers.e),
        }


def _encode_forged(header: Mapping[str, Any], claims: Mapping[str, Any], signature: bytes) -> str:
    h = _b64(json.dumps(header, separators=(",", ":")).encode())
    p = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{h}.{p}.{_b64(signature)}"


def mint_token(
    key: KeyPair,
    *,
    roles: Sequence[str] | None = (),
    aud: str = DEFAULT_AUDIENCE,
    iss: str | None = None,
    oid: str = DEFAULT_OID,
    exp_offset: int = 3600,
    nbf_offset: int = 0,
    kid: str | None = None,
    alg: str = "RS256",
    now: float | None = None,
    extra_claims: Mapping[str, Any] | None = None,  # Any: arbitrary JSON claim values
    tenant_id: str = TEST_TENANT_ID,
    hs256_secret: bytes | None = None,
) -> str:
    """Mint a JWT shaped like an Entra v2 access token.

    Args:
        key: Signing key (for ``alg=HS256`` its *public* PEM is used as the HMAC secret, i.e.
            the classic algorithm-confusion attack).
        roles: Value of the ``roles`` claim; ``None`` omits the claim entirely.
        aud: Audience claim.
        iss: Issuer claim; defaults to :func:`issuer_for` ``tenant_id``.
        oid: Object id claim.
        exp_offset: Seconds from ``now`` until expiry (negative = already expired).
        nbf_offset: Seconds from ``now`` until not-before (positive = not yet valid).
        kid: Header ``kid`` override (defaults to ``key.kid``).
        alg: ``RS256`` (default), ``none`` or ``HS256``.
        now: Epoch seconds used as the issue time (defaults to the current time).
        extra_claims: Extra claims merged last.
        tenant_id: Tenant used for the default ``iss`` and for ``tid`` (so they always agree).
        hs256_secret: HMAC secret for ``alg=HS256`` (defaults to the public key PEM).

    Returns:
        The compact serialised token.
    """
    if isinstance(roles, str):
        raise TypeError("roles must be a sequence of strings, not a single str")
    issued = int(time.time() if now is None else now)
    claims: dict[str, Any] = {  # Any: heterogeneous JSON claim values
        "iss": iss if iss is not None else issuer_for(tenant_id),
        "aud": aud,
        "oid": oid,
        "sub": oid,
        "tid": tenant_id,
        "iat": issued,
        "nbf": issued + nbf_offset,
        "exp": issued + exp_offset,
    }
    if roles is not None:
        claims["roles"] = list(roles)
    claims.update(extra_claims or {})
    header_kid = kid if kid is not None else key.kid
    if alg == "RS256":
        return jwt.encode(claims, key.private_pem(), algorithm="RS256", headers={"kid": header_kid})
    header = {"typ": "JWT", "alg": alg, "kid": header_kid}
    if alg == "none":
        return _encode_forged(header, claims, b"")
    if alg == "HS256":
        signing_input = _encode_forged(header, claims, b"").rsplit(".", 1)[0]
        mac = hmac.new(
            hs256_secret or key.public_pem(), signing_input.encode(), hashlib.sha256
        ).digest()
        return _encode_forged(header, claims, mac)
    raise ValueError(f"unsupported alg {alg!r}")


@dataclass(frozen=True)
class JwksFake:
    """Active + retired keys (both published) and a ``rogue`` key that is never published."""

    active: KeyPair
    retired: KeyPair
    rogue: KeyPair
    tenant_id: str = TEST_TENANT_ID

    @classmethod
    def create(cls, tenant_id: str = TEST_TENANT_ID) -> "JwksFake":
        """Generate a fresh set of keys."""
        return cls(
            KeyPair.generate("test-key-active"),
            KeyPair.generate("test-key-retired"),
            KeyPair.generate("test-key-rogue"),
            tenant_id,
        )

    @property
    def issuer(self) -> str:
        """The v2 issuer of this fake tenant."""
        return issuer_for(self.tenant_id)

    @property
    def url(self) -> str:
        """The JWKS URL of this fake tenant."""
        return jwks_url(self.tenant_id)

    def jwks(self, *, include_retired: bool = True) -> dict[str, list[dict[str, str]]]:
        """JWKS document; the retired key stays published during the rotation overlap."""
        keys = [self.active.public_jwk()]
        if include_retired:
            keys.append(self.retired.public_jwk())
        return {"keys": keys}

    def transport(self, *, include_retired: bool = True) -> httpx.MockTransport:
        """Mock transport serving :meth:`jwks` at :attr:`url` (404 elsewhere); no network."""
        body = self.jwks(include_retired=include_retired)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and str(request.url) == self.url:
                return httpx.Response(200, json=body)
            return httpx.Response(404, json={"error": "not_found"})

        return httpx.MockTransport(handler)

    def client(self) -> httpx.Client:
        """Sync httpx client wired to :meth:`transport`."""
        return httpx.Client(transport=self.transport())

    def async_client(self) -> httpx.AsyncClient:
        """Async httpx client wired to :meth:`transport`."""
        return httpx.AsyncClient(transport=self.transport())

    def mint_token(self, **kwargs: Any) -> str:  # Any: forwarded to module-level mint_token
        """Mint a token signed by the active key; kwargs as :func:`mint_token`.

        Accepts an extra ``key=`` to sign with another :class:`KeyPair`.
        """
        kwargs.setdefault("tenant_id", self.tenant_id)
        key = kwargs.pop("key", self.active)
        return mint_token(key, **kwargs)

    # Negative cases (SPEC-04 section 4). ``**kw: Any`` below is forwarded verbatim to
    # mint_token, whose parameters have heterogeneous types.
    # Negative cases (SPEC-04 section 4) -----------------------------------------------------
    def expired_token(self, **kw: Any) -> str:
        """Token whose ``exp`` is in the past."""
        return self.mint_token(exp_offset=-3600, nbf_offset=-7200, **kw)

    def not_yet_valid_token(self, **kw: Any) -> str:
        """Token whose ``nbf`` is in the future."""
        return self.mint_token(nbf_offset=3600, exp_offset=7200, **kw)

    def wrong_audience_token(self, **kw: Any) -> str:
        """Token for a different audience."""
        return self.mint_token(aud="api://someone-else", **kw)

    def wrong_issuer_token(self, **kw: Any) -> str:
        """Token from a different tenant's issuer."""
        return self.mint_token(tenant_id=FOREIGN_TENANT_ID, **kw)

    def unknown_kid_token(self, **kw: Any) -> str:
        """Validly signed token whose key (and kid) is not in the JWKS."""
        return self.mint_token(key=self.rogue, **kw)

    def alg_none_token(self, **kw: Any) -> str:
        """Unsigned ``alg=none`` token carrying the active kid."""
        return self.mint_token(alg="none", **kw)

    def hs256_confusion_token(self, **kw: Any) -> str:
        """HS256 token MACed with the active public key PEM as the secret."""
        return self.mint_token(alg="HS256", **kw)

    def hs256_confusion_der_token(self, **kw: Any) -> str:
        """HS256 token MACed with the DER form of the active public key."""
        return self.mint_token(alg="HS256", hs256_secret=self.active.public_der(), **kw)

    def hs256_confusion_jwk_token(self, **kw: Any) -> str:
        """HS256 token MACed with the JSON-serialised public JWK."""
        secret = json.dumps(self.active.public_jwk()).encode()
        return self.mint_token(alg="HS256", hs256_secret=secret, **kw)

    def missing_roles_token(self, **kw: Any) -> str:
        """Valid token without a ``roles`` claim."""
        return self.mint_token(roles=None, **kw)
