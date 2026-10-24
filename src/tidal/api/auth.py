"""Entra ID JWT validation (SPEC-04 §2, SPEC-06 §1) and the cursor-key source.

``JwtVerifier`` checks signature (RS256 only, key chosen by ``kid`` from a cached JWKS), ``iss``
(v2.0 issuer of the configured tenant), ``tid``, ``aud``, ``exp`` and ``nbf`` (30 s leeway).
``EntraApiValidator`` turns the claims of a user token into a :class:`Principal`;
``EntraTokenValidator`` (used by ``tidal-query``) accepts only the ``id-api`` workload identity.

The JWKS is cached for one hour; an unknown ``kid`` triggers one refresh (rate limited) so key
rotation is picked up without a restart. If a refresh fails the stale keys keep being used.
``TIDAL_AUTH__JWKS_URL`` (local only) may be an http(s) URL or a file path. Tokens are never
logged; every rejection is a generic ``Unauthenticated``.
"""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol  # Any: JWT claims are arbitrary JSON

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from tidal.api.deps import ALL_ROLES, Principal
from tidal.common.config import Settings
from tidal.common.errors import ConfigError, DependencyUnavailable, Forbidden, Unauthenticated
from tidal.common.logging import get_logger

LEEWAY_SECONDS: Final = 30
JWKS_TTL_SECONDS: Final = 3600.0
JWKS_MIN_REFRESH_SECONDS: Final = 30.0
_DENIED: Final = "The bearer token is invalid."
_NOT_ALLOWED: Final = "The caller is not allowed to use this service."

logger = get_logger(__name__)


def issuer_for(tenant_id: str) -> str:
    """Entra v2.0 issuer of ``tenant_id``."""
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def default_jwks_url(tenant_id: str) -> str:
    """Entra v2.0 signing-key endpoint of ``tenant_id``."""
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


class JwksCache:
    """Async JWKS fetcher with a TTL and rate-limited refresh on unknown ``kid``."""

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """``url`` is http(s) or (local only) a file path; ``client`` is injectable for tests."""
        self._url = url
        self._client = client
        self._owns_client = client is None
        self._monotonic = monotonic
        self._keys: dict[str, Any] = {}  # Any: cryptography public key objects
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def _fetch(self) -> list[dict[str, Any]]:
        if self._url.startswith(("http://", "https://")):
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
            response = await self._client.get(self._url)
            response.raise_for_status()
            document = response.json()
        else:
            document = json.loads(await asyncio.to_thread(Path(self._url).read_text, "utf-8"))
        keys = document["keys"]
        if not isinstance(keys, list):
            raise ValueError("malformed JWKS")
        return [k for k in keys if isinstance(k, dict)]

    async def _refresh(self) -> None:
        self._last_attempt = self._monotonic()
        try:
            jwks = await self._fetch()
            keys: dict[str, Any] = {}
            for jwk in jwks:
                kid = jwk.get("kid")
                if jwk.get("kty") == "RSA" and isinstance(kid, str):
                    keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, jwt.PyJWTError):
            logger.warning("jwks_refresh_failed")
            return  # keep serving the stale keys
        self._keys = keys
        self._fetched_at = self._monotonic()

    async def key(self, kid: str) -> Any:  # Any: cryptography public key
        """The public key for ``kid``; refreshes when stale or unknown (at most every 30 s)."""
        async with self._lock:
            now = self._monotonic()
            stale = self._fetched_at is None or now - self._fetched_at >= JWKS_TTL_SECONDS
            can_retry = (
                self._last_attempt is None or now - self._last_attempt >= JWKS_MIN_REFRESH_SECONDS
            )
            if (stale or kid not in self._keys) and can_retry:
                await self._refresh()
            if self._fetched_at is None:
                raise DependencyUnavailable("Signing keys are unavailable.")
            if kid not in self._keys:
                raise Unauthenticated(_DENIED)
            return self._keys[kid]

    async def aclose(self) -> None:
        """Close the HTTP client when this cache created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()


class JwtVerifier:
    """Signature + standard-claim validation against one tenant and one audience."""

    def __init__(self, tenant_id: str, audience: str | list[str], jwks: JwksCache) -> None:
        """Bind the verifier to ``tenant_id`` (iss, tid) and the accepted ``audience`` value(s)."""
        self._tenant = tenant_id.lower()
        self._audience = audience
        self._jwks = jwks

    async def verify(self, token: str) -> dict[str, Any]:  # Any: JSON claims
        """Return the verified claims or raise ``Unauthenticated`` (generic message)."""
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str):
                raise Unauthenticated(_DENIED)
            key = await self._jwks.key(kid)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=issuer_for(self._tenant),
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "nbf", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            raise Unauthenticated(_DENIED) from None
        tid = claims.get("tid")
        if not isinstance(tid, str) or tid.lower() != self._tenant:
            raise Unauthenticated(_DENIED)
        return claims


def _roles(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("roles")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(r for r in raw if isinstance(r, str))


class PrincipalValidator(Protocol):
    """Turns a bearer token into a :class:`Principal`."""

    async def validate(self, token: str) -> Principal:
        """Return the principal or raise ``Unauthenticated``."""
        ...


class RejectingValidator:
    """Fail-closed validator used when no tenant is configured (local, auth enabled)."""

    async def validate(self, token: str) -> Principal:  # noqa: ARG002 - protocol signature
        """Always reject."""
        raise Unauthenticated("Token validation is not available.")


class EntraApiValidator:
    """Validator for end-user / client tokens sent to ``tidal-api``."""

    def __init__(self, verifier: JwtVerifier) -> None:
        """Wrap ``verifier`` (audience ``TIDAL_AUTH__AUDIENCE``)."""
        self._verifier = verifier

    async def validate(self, token: str) -> Principal:
        """Verified principal; only the four Tidal roles are kept from the ``roles`` claim."""
        claims = await self._verifier.verify(token)
        oid = claims.get("oid")
        if not isinstance(oid, str) or not oid:
            raise Unauthenticated(_DENIED)
        return Principal(oid=oid, roles=_roles(claims) & ALL_ROLES)


class EntraTokenValidator:
    """``tidal-query`` caller check: audience, ``Tidal.Query.Read`` role and a caller allowlist.

    The caller (``id-api``, ``id-worker``) must hold the role AND have ``azp`` (or v1 ``appid``)
    in ``client_ids`` OR ``oid`` in ``object_ids``.
    """

    def __init__(
        self,
        verifier: JwtVerifier,
        *,
        required_role: str,
        client_ids: frozenset[str],
        object_ids: frozenset[str],
    ) -> None:
        """Both allowlists empty is a startup ``ConfigError`` (fail closed)."""
        if not client_ids and not object_ids:
            raise ConfigError(
                "tidal-query needs TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS "
                "or TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS"
            )
        self._verifier = verifier
        self._role = required_role
        self._client_ids = frozenset(c.lower() for c in client_ids)
        self._object_ids = frozenset(o.lower() for o in object_ids)

    async def validate(self, token: str) -> None:
        """Raise ``Unauthenticated`` / ``Forbidden`` unless the caller is allowlisted."""
        claims = await self._verifier.verify(token)
        if self._role not in _roles(claims):
            raise Forbidden(_NOT_ALLOWED)
        azp = claims.get("azp", claims.get("appid"))
        oid = claims.get("oid")
        by_client = isinstance(azp, str) and azp.lower() in self._client_ids
        by_object = isinstance(oid, str) and oid.lower() in self._object_ids
        if not (by_client or by_object):
            raise Forbidden(_NOT_ALLOWED)


def _verifier(
    settings: Settings, audiences: list[str], client: httpx.AsyncClient | None
) -> JwtVerifier:
    tenant = settings.auth.tenant_id
    if tenant is None:
        raise ConfigError("TIDAL_AUTH__TENANT_ID is required to validate tokens")
    url = settings.auth.jwks_url or default_jwks_url(str(tenant))
    return JwtVerifier(str(tenant), audiences, JwksCache(url, client))


def _audiences(uri: str, client_id: str | None) -> list[str]:
    """Accepted ``aud`` values: the app id URI and (v2 tokens) the app client id GUID."""
    return [uri, client_id] if client_id else [uri]


def api_validator_from_settings(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> PrincipalValidator:
    """JWT validator for ``tidal-api``; rejects everything when no tenant is configured."""
    if settings.auth.tenant_id is None:
        return RejectingValidator()
    return EntraApiValidator(
        _verifier(
            settings, _audiences(settings.auth.audience, settings.auth.api_app_client_id), client
        )
    )


def query_validator_from_settings(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> EntraTokenValidator:
    """``tidal-query`` caller validator (audience ``TIDAL_QUERY__AUDIENCE``)."""
    return EntraTokenValidator(
        _verifier(
            settings, _audiences(settings.query.audience, settings.query.app_client_id), client
        ),
        required_role=settings.auth.query_role,
        client_ids=settings.auth.caller_client_ids(),
        object_ids=settings.auth.caller_object_ids(),
    )


# -- cursor HMAC key ----------------------------------------------------------------------------
MIN_CURSOR_KEY_BYTES: Final = 32


class SecretSource(Protocol):
    """Reads a secret value by name (Key Vault in Azure, a fake in tests)."""

    def get(self, name: str) -> str:
        """Return the secret's value."""
        ...


class KeyVaultSecretSource:
    """Key Vault through ``azure-keyvault-secrets`` with the workload's Managed Identity."""

    def __init__(self, vault_url: str, credential: Any = None) -> None:  # Any: azure credential
        """``credential`` defaults to ``DefaultAzureCredential`` (honours ``AZURE_CLIENT_ID``)."""
        self._vault_url = vault_url
        self._credential = credential

    def get(self, name: str) -> str:
        """Fetch the latest secret version."""
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
        from azure.keyvault.secrets import SecretClient  # noqa: PLC0415

        credential = self._credential or DefaultAzureCredential()
        client = SecretClient(vault_url=self._vault_url, credential=credential)
        value = client.get_secret(name).value
        if value is None:
            raise ConfigError("the cursor key secret has no value")
        return str(value)


def _checked(value: str) -> bytes:
    raw = value.encode()
    if len(raw) < MIN_CURSOR_KEY_BYTES:
        raise ConfigError(f"the cursor key must be at least {MIN_CURSOR_KEY_BYTES} bytes")
    return raw


def resolve_cursor_key(settings: Settings, source: SecretSource | None = None) -> bytes | None:
    """The shared cursor HMAC key, or ``None`` for a per-process random key (local only).

    Local: ``TIDAL_CURSOR__KEY`` if set, else ``None``. Elsewhere the key must come from Key
    Vault (``TIDAL_CURSOR__KEYVAULT_URL``); no key or a short key is a startup ``ConfigError``.
    """
    cursor = settings.cursor
    if cursor.key is not None:
        return _checked(cursor.key.get_secret_value())
    if cursor.keyvault_url is None:
        if settings.env == "local":
            return None
        raise ConfigError(f"TIDAL_CURSOR__KEYVAULT_URL is required when TIDAL_ENV={settings.env}")
    source = source or KeyVaultSecretSource(cursor.keyvault_url)
    try:
        value = source.get(cursor.secret_name)
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("the cursor key could not be read from Key Vault") from None
    return _checked(value)
