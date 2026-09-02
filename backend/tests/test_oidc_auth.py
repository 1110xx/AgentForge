"""OIDC AuthContextProvider tests (security/oidc.py, Phase 5 Step 1).

Offline end-to-end: a local RSA keypair plays the IdP; a httpx.MockTransport
serves the OIDC discovery document + JWKS so every verification path
(discovery, key selection, RS256 verify, claim validation/mapping) is exercised
without any real identity provider or network.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    generate_private_key,
)

from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.integration.host import HostPortError
from enterprise_agent_platform.reference.local_stack import ReferenceLocalAuth
from enterprise_agent_platform.security.oidc import (
    OIDCAuthContextProvider,
    create_auth_provider_from_env,
)

ISSUER = "https://idp.example.test/"
AUDIENCE = "agent-platform-api"

KID = "test-key-1"


@dataclass
class FakeIdp:
    key: RSAPrivateKey
    public_jwk: dict[str, Any]
    discovery_requests: int = 0
    jwks_requests: int = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/.well-known/openid-configuration"):
                self.discovery_requests += 1
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "jwks_uri": "https://idp.example.test/jwks",
                    },
                )
            if request.url.path.endswith("/jwks"):
                self.jwks_requests += 1
                return httpx.Response(200, json={"keys": [self.public_jwk]})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def issue_token(
        self,
        *,
        claims: dict[str, Any] | None = None,
        expires_in: int = 600,
        kid: str = KID,
        alg: str = "RS256",
        issuer: str = ISSUER.rstrip("/"),
        audience: str | list[str] = AUDIENCE,
    ) -> str:
        base = {
            "iss": issuer,
            "sub": "analyst-alice",
            "aud": audience,
            "tenant_id": "tenant-demo",
            "scope": "runs:create runs:read",
            "iat": int(time.time()),
            "exp": int(time.time()) + expires_in,
        }
        if claims:
            base.update(claims)
        header = {"alg": alg, "typ": "JWT", "kid": kid}
        encoded_header = _b64(json.dumps(header, separators=(",", ":")))
        encoded_payload = _b64(json.dumps(base, separators=(",", ":")))
        signing_input = f"{encoded_header}.{encoded_payload}".encode()
        if alg == "RS256":
            signature = self.key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "HS256":
            signature = b"\x00" * 32
        elif alg == "none":
            signature = b""
        else:
            signature = b"\x00" * 64
        return f"{encoded_header}.{encoded_payload}.{_b64(signature)}"


def _b64(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _make_idp() -> FakeIdp:
    key = generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    public_jwk = {
        "kty": "RSA",
        "kid": KID,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    return FakeIdp(key=key, public_jwk=public_jwk)


def _provider(idp: FakeIdp, **overrides: Any) -> tuple[OIDCAuthContextProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=idp.transport())
    provider = OIDCAuthContextProvider(
        issuer=ISSUER,
        audience=AUDIENCE,
        http_client=client,
        **overrides,
    )
    return provider, client


def _run(coro):
    return asyncio.run(coro)


async def _authenticate(
    provider: OIDCAuthContextProvider, token: str | None, authorization_prefix: str = "Bearer "
) -> RequestContext:
    authorization = f"{authorization_prefix}{token}" if token is not None else None
    return await provider.authenticate(authorization, request_id="req-1", trace_id="trace-1")


def test_valid_token_maps_claims_to_request_context() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        token = idp.issue_token()
        ctx = _run(_authenticate(provider, token))
        assert isinstance(ctx, RequestContext)
        assert ctx.tenant_id == "tenant-demo"
        assert ctx.actor_id == "analyst-alice"
        assert set(ctx.scopes) == {"runs:create", "runs:read"}
        assert ctx.request_id == "req-1"
        assert ctx.trace_id == "trace-1"
    finally:
        _run(client.aclose())


def test_scopes_list_claim_and_custom_claims() -> None:
    idp = _make_idp()
    provider, client = _provider(
        idp,
        tenant_claims=("custom_tenant",),
        actor_claim="email",
        scope_claims=("my_scopes",),
    )
    try:
        token = idp.issue_token(
            claims={
                "custom_tenant": "tenant-b",
                "email": "alice@corp.example",
                "my_scopes": ["runs:create", "runs:cancel"],
            }
        )
        ctx = _run(_authenticate(provider, token))
        assert ctx.tenant_id == "tenant-b"
        assert ctx.actor_id == "alice@corp.example"
        assert set(ctx.scopes) == {"runs:create", "runs:cancel"}
    finally:
        _run(client.aclose())


def test_missing_authorization_and_garbage_are_rejected() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        for token, prefix in ((None, "Bearer "), ("", "Bearer "), ("not-a-jwt", "Bearer ")):
            with pytest.raises(HostPortError) as error:
                _run(_authenticate(provider, token, prefix))
            assert error.value.code == "UNAUTHENTICATED"
        with pytest.raises(HostPortError) as error:
            _run(provider.authenticate("Basic abc", "req-1", "trace-1"))
        assert error.value.code == "UNAUTHENTICATED"
    finally:
        _run(client.aclose())


def test_wrong_kid_and_single_key_without_kid() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        token = idp.issue_token(kid="unknown-key")
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, token))
        assert error.value.code == "UNAUTHENTICATED"
    finally:
        _run(client.aclose())

    # A kid-less token still verifies when the JWKS holds exactly one RSA key.
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        token = idp.issue_token(claims={}, kid="")
        token = token.replace('"kid":""', "", 1).replace('", "typ"', '"typ"', 1)
        # simpler: craft header without kid
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss": ISSUER.rstrip("/"),
            "sub": "analyst-alice",
            "aud": AUDIENCE,
            "tenant_id": "tenant-demo",
            "scope": "runs:read",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        }
        signing = f"{_b64(json.dumps(header))}.{_b64(json.dumps(payload))}"
        sig = idp.key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        token = f"{signing}.{_b64(sig)}"
        ctx = _run(_authenticate(provider, token))
        assert ctx.actor_id == "analyst-alice"
    finally:
        _run(client.aclose())


def test_expired_and_not_yet_valid_tokens_are_rejected() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        expired = idp.issue_token(expires_in=-120)
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, expired))
        assert error.value.code == "UNAUTHENTICATED"

        future = idp.issue_token(
            claims={"nbf": int(time.time()) + 600, "exp": int(time.time()) + 1200}
        )
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, future))
        assert error.value.code == "UNAUTHENTICATED"
    finally:
        _run(client.aclose())


def test_audience_and_issuer_mismatches_are_rejected() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        wrong_aud = idp.issue_token(audience="another-api")
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, wrong_aud))
        assert error.value.code == "UNAUTHENTICATED"

        wrong_iss = idp.issue_token(issuer="https://evil.example")
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, wrong_iss))
        assert error.value.code == "UNAUTHENTICATED"
    finally:
        _run(client.aclose())


def test_non_rs256_algorithms_are_rejected() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        for alg in ("none", "HS256"):
            token = idp.issue_token(alg=alg)
            with pytest.raises(HostPortError) as error:
                _run(_authenticate(provider, token))
            assert error.value.code == "UNAUTHENTICATED"
    finally:
        _run(client.aclose())


def test_tampered_signature_is_rejected() -> None:
    idp = _make_idp()
    provider, client = _provider(idp)
    try:
        token = idp.issue_token()
        parts = token.split(".")
        tampered = f"{parts[0]}.{_b64(b'{\"x\":1}')}.{parts[2]}"
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, tampered))
        assert error.value.code == "UNAUTHENTICATED"
    finally:
        _run(client.aclose())


def test_jwks_and_discovery_are_cached_within_ttl() -> None:
    idp = _make_idp()
    provider, client = _provider(idp, discovery_cache_ttl_seconds=300)
    try:
        token = idp.issue_token()
        for _ in range(3):
            _run(_authenticate(provider, token))
        assert idp.discovery_requests == 1, "discovery must be fetched once"
        assert idp.jwks_requests == 1, "jwks must be fetched once"
    finally:
        _run(client.aclose())


def test_jwks_uri_override_skips_discovery() -> None:
    idp = _make_idp()
    client = httpx.AsyncClient(transport=idp.transport())
    provider = OIDCAuthContextProvider(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="https://idp.example.test/jwks",
        http_client=client,
    )
    try:
        token = idp.issue_token()
        ctx = _run(_authenticate(provider, token))
        assert ctx.actor_id == "analyst-alice"
        assert idp.discovery_requests == 0
        assert idp.jwks_requests == 1
    finally:
        _run(client.aclose())


def test_unavailable_idp_is_retryable_host_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OIDCAuthContextProvider(
        issuer=ISSUER, audience=AUDIENCE, http_client=client
    )
    try:
        token = _make_idp().issue_token()
        with pytest.raises(HostPortError) as error:
            _run(_authenticate(provider, token))
        assert error.value.code == "HOST_PORT_UNAVAILABLE"
        assert error.value.retryable is True
    finally:
        _run(client.aclose())


def test_from_env_requires_issuer_and_audience() -> None:
    with pytest.raises(ValueError):
        OIDCAuthContextProvider.from_env(
            {"AGENT_PLATFORM_OIDC_ISSUER": ISSUER, "AGENT_PLATFORM_OIDC_AUDIENCE": ""}
        )
    provider = OIDCAuthContextProvider.from_env(
        {
            "AGENT_PLATFORM_OIDC_ISSUER": ISSUER,
            "AGENT_PLATFORM_OIDC_AUDIENCE": AUDIENCE,
        }
    )
    assert isinstance(provider, OIDCAuthContextProvider)


def test_auth_provider_selection_env(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_AUTH_PROVIDER", raising=False)
    assert isinstance(create_auth_provider_from_env(), ReferenceLocalAuth)
    monkeypatch.setenv("AGENT_PLATFORM_AUTH_PROVIDER", "reference")
    assert isinstance(create_auth_provider_from_env(), ReferenceLocalAuth)
    monkeypatch.setenv("AGENT_PLATFORM_AUTH_PROVIDER", "oidc")
    with pytest.raises(ValueError):
        create_auth_provider_from_env()
    monkeypatch.setenv("AGENT_PLATFORM_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("AGENT_PLATFORM_OIDC_AUDIENCE", AUDIENCE)
    assert isinstance(create_auth_provider_from_env(), OIDCAuthContextProvider)
