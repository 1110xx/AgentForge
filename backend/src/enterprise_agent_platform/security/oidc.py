"""OIDC ``AuthContextProvider`` (production-prerequisite Step 1, Phase 5).

The platform authenticates external ``/v1`` API callers through the single
``AuthContextProvider`` port (``integration/host.py``). This module implements
that port against a standards OIDC issuer:

* discovery — ``GET {issuer}/.well-known/openid-configuration`` → ``jwks_uri``
  (or an explicit ``jwks_uri`` override, skipping discovery);
* JWKS fetch + cache (TTL-bounded, see ``discovery_cache_ttl_seconds``);
* RS256 signature verification of the Bearer JWT with the JWK matching the
  token ``kid`` (cryptography ``RSAPublicNumbers`` — no extra dependency);
* claim validation — exact issuer, audience, ``exp``/``nbf`` with leeway;
* claim → ``RequestContext`` mapping — actor (``sub`` by default), tenant and
  scopes from configurable claims (see ``tenant_claims`` / ``scope_claims``).

The reference/dev fallback (``ReferenceLocalAuth`` — static bearer) is the
default everywhere; OIDC is enabled explicitly in the K8s API factory
(``reference/k8s_container.create_container``) via env selection:

* ``AGENT_PLATFORM_AUTH_PROVIDER=oidc`` — opt in
* ``AGENT_PLATFORM_OIDC_ISSUER`` / ``AGENT_PLATFORM_OIDC_AUDIENCE`` — required
* ``AGENT_PLATFORM_OIDC_JWKS_URI`` — optional discovery override
* ``AGENT_PLATFORM_OIDC_TENANT_CLAIM`` / ``AGENT_PLATFORM_OIDC_SCOPE_CLAIM``
  — optional claim mapping (defaults below)

Contract (also mirrored in docs/security.md §6): the IdP's ``scope`` (or a
custom scopes claim) names the platform scopes (``runs:create`` …) verbatim;
tenant and actor claims are tenant-boundary inputs — the platform never trusts
client-supplied tenant headers, and every resource/authorization query still
runs under the mapped tenant (SDD §3.3 / §12).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.integration.host import HostPortError

MAX_TOKEN_BYTES = 32 * 1024
_LEEWAY_SECONDS = 30
_DEFAULT_CACHE_TTL_SECONDS = 300
_JWK_ALGORITHM = "RS256"

_SAFE_CLAIM = re.compile(r"[A-Za-z0-9_.:/@-]{1,255}$")


@dataclass(frozen=True, slots=True)
class _OidcKeyset:
    jwks_uri: str
    keys: tuple[dict[str, Any], ...]
    fetched_at: float


class OIDCAuthContextProvider:
    """Verify OIDC RS256 Bearer JWTs and map claims into RequestContext."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | tuple[str, ...],
        jwks_uri: str | None = None,
        discovery_url: str | None = None,
        tenant_claims: tuple[str, ...] = ("tenant_id", "tenant"),
        actor_claim: str = "sub",
        scope_claims: tuple[str, ...] = ("scope", "scopes"),
        leeway_seconds: int = _LEEWAY_SECONDS,
        discovery_cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        now: float | None = None,
    ) -> None:
        if not issuer or not issuer.startswith(("https://", "http://")):
            raise ValueError("issuer must be an absolute https URL")
        self._issuer = issuer.rstrip("/")
        audiences = (audience,) if isinstance(audience, str) else tuple(audience)
        if not audiences or not all(audiences):
            raise ValueError("audience is required")
        self._audiences = audiences
        self._jwks_uri = jwks_uri.rstrip("/") if jwks_uri else None
        self._discovery_url = discovery_url or f"{self._issuer}/.well-known/openid-configuration"
        self._tenant_claims = tenant_claims
        self._actor_claim = actor_claim
        self._scope_claims = scope_claims
        self._leeway_seconds = leeway_seconds
        self._cache_ttl = discovery_cache_ttl_seconds
        self._now = now
        # Lazy client owned by this provider when the caller did not inject one.
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
        )
        self._keyset: _OidcKeyset | None = None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── AuthContextProvider port ─────────────────────────────────────────

    async def authenticate(
        self,
        authorization: str | None,
        request_id: str,
        trace_id: str | None,
    ) -> RequestContext:
        token = _extract_bearer(authorization)
        unverified_header = _decode_unverified_header(token)
        algorithm = unverified_header.get("alg")
        if algorithm != _JWK_ALGORITHM:
            raise HostPortError(
                "UNAUTHENTICATED", f"unsupported JWT algorithm: {algorithm!r}"
            )
        keyset = await self._keyset_for()
        jwk = _select_jwk(keyset.keys, unverified_header.get("kid"))
        claims = await asyncio.to_thread(
            _verify_rs256, token, jwk, self._issuer, self._audiences
        )
        self._validate_times(claims)
        return _map_claims(
            claims,
            tenant_claims=self._tenant_claims,
            actor_claim=self._actor_claim,
            scope_claims=self._scope_claims,
            request_id=request_id,
            trace_id=trace_id,
        )

    # ── internals ────────────────────────────────────────────────────────

    async def _keyset_for(self) -> _OidcKeyset:
        cached = self._keyset
        if cached is not None and (self._now or time.time()) - cached.fetched_at < self._cache_ttl:
            return cached
        jwks_uri = self._jwks_uri
        if jwks_uri is None:
            jwks_uri = await self._discover_jwks_uri()
        keyset = await self._fetch_keyset(jwks_uri)
        self._keyset = keyset
        return keyset

    async def _discover_jwks_uri(self) -> str:
        try:
            response = await self._client.get(self._discovery_url)
        except httpx.HTTPError as error:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                "OIDC discovery is temporarily unavailable",
                retryable=True,
            ) from error
        if response.status_code != 200:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                f"OIDC discovery failed with HTTP {response.status_code}",
                retryable=True,
            )
        try:
            document = response.json()
        except ValueError as error:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE", "OIDC discovery document is invalid"
            ) from error
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise HostPortError(
                "UNAUTHENTICATED", "OIDC discovery document has no jwks_uri"
            )
        return urljoin(self._discovery_url, jwks_uri)

    async def _fetch_keyset(self, jwks_uri: str) -> _OidcKeyset:
        try:
            response = await self._client.get(jwks_uri)
        except httpx.HTTPError as error:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                "OIDC JWKS is temporarily unavailable",
                retryable=True,
            ) from error
        if response.status_code != 200:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                f"OIDC JWKS failed with HTTP {response.status_code}",
                retryable=True,
            )
        try:
            body = response.json()
            keys = body.get("keys")
        except ValueError as error:
            raise HostPortError("UNAUTHENTICATED", "OIDC JWKS document is invalid") from error
        if not isinstance(keys, list) or not keys:
            raise HostPortError("UNAUTHENTICATED", "OIDC JWKS contains no keys")
        return _OidcKeyset(
            jwks_uri=jwks_uri,
            keys=tuple(keys),
            fetched_at=self._now or time.time(),
        )

    def _validate_times(self, claims: dict[str, Any]) -> None:
        current = self._now if self._now is not None else time.time()
        if isinstance(claims.get("nbf"), (int, float)) and current + self._leeway_seconds < int(
            claims["nbf"]
        ):
            raise HostPortError("UNAUTHENTICATED", "JWT is not yet valid")
        if isinstance(claims.get("exp"), (int, float)) and current - self._leeway_seconds > int(
            claims["exp"]
        ):
            raise HostPortError("UNAUTHENTICATED", "JWT has expired")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides: Any) -> OIDCAuthContextProvider:
        """Build from AGENT_PLATFORM_OIDC_* env vars (see module docstring)."""
        source = os.environ if env is None else env
        issuer = source.get("AGENT_PLATFORM_OIDC_ISSUER", "").strip()
        audience = source.get("AGENT_PLATFORM_OIDC_AUDIENCE", "").strip()
        if not issuer or not audience:
            raise ValueError(
                "AGENT_PLATFORM_OIDC_ISSUER and AGENT_PLATFORM_OIDC_AUDIENCE are "
                "required when AGENT_PLATFORM_AUTH_PROVIDER=oidc"
            )
        jwks_uri = source.get("AGENT_PLATFORM_OIDC_JWKS_URI", "").strip() or None
        tenant_claims = tuple(
            claim.strip()
            for claim in source.get("AGENT_PLATFORM_OIDC_TENANT_CLAIM", "tenant_id").split(",")
            if claim.strip()
        )
        actor_claim = source.get("AGENT_PLATFORM_OIDC_ACTOR_CLAIM", "sub").strip() or "sub"
        scope_claims = tuple(
            claim.strip()
            for claim in source.get("AGENT_PLATFORM_OIDC_SCOPE_CLAIM", "scope,scopes").split(",")
            if claim.strip()
        )
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_uri=jwks_uri,
            tenant_claims=tenant_claims,
            actor_claim=actor_claim,
            scope_claims=scope_claims,
            **overrides,
        )


def _extract_bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HostPortError("UNAUTHENTICATED", "authentication failed")
    token = authorization.removeprefix("Bearer ").strip()
    if (
        not token
        or token != token.strip()
        or any(character.isspace() for character in token)
        or len(token.encode()) > MAX_TOKEN_BYTES
    ):
        raise HostPortError("UNAUTHENTICATED", "authentication failed")
    return token


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _decode_unverified_header(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise HostPortError("UNAUTHENTICATED", "JWT is malformed")
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise HostPortError("UNAUTHENTICATED", "JWT header is invalid") from error
    if not isinstance(header, dict):
        raise HostPortError("UNAUTHENTICATED", "JWT header is invalid")
    return header


def _select_jwk(keys: tuple[dict[str, Any], ...], kid: Any) -> dict[str, Any]:
    if isinstance(kid, str) and kid:
        for key in keys:
            if key.get("kid") == kid and key.get("kty") == "RSA":
                return key
        raise HostPortError("UNAUTHENTICATED", "JWT kid is not in the JWKS")
    if len(keys) == 1 and keys[0].get("kty") == "RSA":
        return keys[0]
    raise HostPortError("UNAUTHENTICATED", "JWT is missing the kid claim")


def _b64url_int(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), byteorder="big")


def _verify_rs256(
    token: str,
    jwk: dict[str, Any],
    issuer: str,
    audiences: tuple[str, ...],
) -> dict[str, Any]:
    parts = token.split(".")
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        signature = _b64url_decode(parts[2])
        public_key = RSAPublicNumbers(
            e=_b64url_int(str(jwk["e"])), n=_b64url_int(str(jwk["n"]))
        ).public_key()
        public_key.verify(
            signature,
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, InvalidSignature) as error:
        raise HostPortError("UNAUTHENTICATED", "JWT signature verification failed") from error
    if not isinstance(payload, dict):
        raise HostPortError("UNAUTHENTICATED", "JWT payload is invalid")
    if payload.get("iss") != issuer:
        raise HostPortError("UNAUTHENTICATED", "JWT issuer mismatch")
    token_aud = payload.get("aud")
    token_audiences = (
        (token_aud,) if isinstance(token_aud, str) else (tuple(token_aud) if isinstance(token_aud, list) else ())
    )
    if not any(aud in audiences for aud in token_audiences):
        raise HostPortError("UNAUTHENTICATED", "JWT audience mismatch")
    return payload


def _safe_claim(value: Any) -> str:
    text = str(value)
    if not _SAFE_CLAIM.fullmatch(text):
        raise HostPortError("UNAUTHENTICATED", "JWT claim is invalid")
    return text


def _map_claims(
    claims: dict[str, Any],
    *,
    tenant_claims: tuple[str, ...],
    actor_claim: str,
    scope_claims: tuple[str, ...],
    request_id: str,
    trace_id: str | None,
) -> RequestContext:
    tenant_id: str | None = None
    for claim in tenant_claims:
        value = claims.get(claim)
        if isinstance(value, str) and value:
            tenant_id = _safe_claim(value)
            break
    if tenant_id is None:
        raise HostPortError("UNAUTHENTICATED", "JWT carries no tenant claim")
    actor_value = claims.get(actor_claim)
    if not isinstance(actor_value, str) or not actor_value:
        raise HostPortError("UNAUTHENTICATED", "JWT carries no actor (sub) claim")
    actor_id = _safe_claim(actor_value)
    scopes: list[str] = []
    for claim in scope_claims:
        value = claims.get(claim)
        if isinstance(value, str):
            scopes.extend(part for part in value.split() if part)
        elif isinstance(value, list):
            scopes.extend(_safe_claim(part) for part in value if isinstance(part, str))
    return RequestContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        scopes=tuple(dict.fromkeys(scopes)),
        request_id=request_id,
        trace_id=trace_id,
    )


__all__ = [
    "OIDCAuthContextProvider",
    "create_auth_provider_from_env",
]


def create_auth_provider_from_env(env: dict[str, str] | None = None) -> Any:
    """Select the API auth provider: ``oidc`` when opted in, reference otherwise.

    Used by the K8s API factory (reference/k8s_container.create_container):
    the reference static-bearer provider stays the default so disposable
    gates keep running with zero external identity infrastructure; OIDC is the
    production path (AGENT_PLATFORM_AUTH_PROVIDER=oidc).
    """
    source = os.environ if env is None else env
    provider = source.get("AGENT_PLATFORM_AUTH_PROVIDER", "").strip().lower()
    if provider == "oidc":
        return OIDCAuthContextProvider.from_env(source)
    from enterprise_agent_platform.reference.local_stack import ReferenceLocalAuth

    return ReferenceLocalAuth()
