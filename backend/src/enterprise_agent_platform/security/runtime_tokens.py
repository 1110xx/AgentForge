"""HMAC-SHA256 signed short-lived Runtime capability tokens.

Replaces the legacy deterministic plaintext derivation
``runtime-token:{attempt_id}`` (``fastapi/internal_adapter.py`` — knowing an
attempt_id yielded the exact bearer, with no signature and no expiry) with a
compact signed capability:

    rt.v1.<base64url(payload-json)>.<hex(hmac-sha256(key, payload-b64url))>

where the payload binds the subject (attempt_id) plus ``iat``/``exp`` unix
timestamps. The signature makes forgery impossible without the key; ``exp``
bounds the token lifetime so a leaked token cannot outlive its window
(verification in the Control-Plane API, issuance at bootstrap / heartbeat
refresh — SDD §6.1 + docs/phase-4.5-security-decisions.md §2.3).

Key source — ``AGENT_PLATFORM_CAPABILITY_KEY`` environment variable:

* production / K8s — injected by the SecretStore (Vault → ExternalSecret →
  envFrom on the API deployment, see deploy/helm + bootstrap-prod-wiring.sh);
* local / demo / in-memory — falls back to a **static demo key** so dev runs,
  tests and the kind gate keep working across processes/replicas with zero
  secret plumbing. The demo key is not a secret: production must always inject
  ``AGENT_PLATFORM_CAPABILITY_KEY`` (the API's secret contract now includes it).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from enterprise_agent_platform.persistence.protocol import PlatformError

logger = logging.getLogger(__name__)

CAPABILITY_KEY_ENV = "AGENT_PLATFORM_CAPABILITY_KEY"
# Static dev/demo fallback (documented, NOT a secret). In-memory stores, local
# subprocess runs and the disposable kind gate all share this value so both
# sides of the Internal API (bootstrap issues, ops verify) agree without any
# SecretStore. Production injects a real key through the SecretStore.
DEMO_CAPABILITY_KEY = "agent-platform-demo-capability-key-change-me-in-production"

TOKEN_PREFIX = "rt.v1."
# Version inside the payload (future-proof claim evolution).
_PAYLOAD_VERSION = 1
# Default lifetime when the issuer does not override: matches the historical
# runtime-token validity window (_RUNTIME_TTL_SECONDS in internal_adapter).
DEFAULT_RUNTIME_TOKEN_TTL_SECONDS = 300
# Max token length guard (bounded memory / header validation).
_MAX_TOKEN_BYTES = 512
# Clock-skew tolerance accepted on both iat and exp.
_LEEWAY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class RuntimeTokenClaims:
    attempt_id: str
    issued_at: datetime
    expires_at: datetime


def resolve_capability_key(env: dict[str, str] | None = None) -> str:
    """Return the signing key: SecretStore env first, demo fallback otherwise."""
    source = os.environ if env is None else env
    configured = source.get(CAPABILITY_KEY_ENV, "").strip()
    if configured:
        return configured
    logger.warning(
        "%s is not set — using the static DEMO capability key (dev/demo only; "
        "production must inject a real key via the SecretStore)",
        CAPABILITY_KEY_ENV,
    )
    return DEMO_CAPABILITY_KEY


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_runtime_token(
    attempt_id: str,
    *,
    key: str | None = None,
    ttl_seconds: int = DEFAULT_RUNTIME_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """Issue a signed, expiring Runtime capability bound to ``attempt_id``."""
    if not attempt_id:
        raise ValueError("attempt_id is required")
    signing_key = key if key is not None else resolve_capability_key()
    current = now or datetime.now(UTC)
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    payload = {
        "v": _PAYLOAD_VERSION,
        "sub": attempt_id,
        "iat": int(current.timestamp()),
        "exp": int(current.timestamp()) + ttl_seconds,
    }
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign(encoded, signing_key)
    return f"{TOKEN_PREFIX}{encoded}.{signature}"


def _sign(payload_b64url: str, key: str) -> str:
    digest = hmac.new(
        key.encode("utf-8"), payload_b64url.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return digest


def verify_runtime_token(
    token: str,
    *,
    attempt_id: str,
    key: str | None = None,
    now: datetime | None = None,
) -> RuntimeTokenClaims:
    """Verify signature, expiry and subject binding of a Runtime capability.

    Raises :class:`PlatformError` with codes mapped to 401 by both the platform
    and standalone Internal API error handlers:

    * ``AUTH_FAILED``  — malformed / forged / subject-mismatched token
    * ``AUTH_EXPIRED`` — signature valid but outside the iat..exp window
    """
    signing_key = key if key is not None else resolve_capability_key()
    if (
        not isinstance(token, str)
        or not token
        or not token.startswith(TOKEN_PREFIX)
        or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
    ):
        raise PlatformError("AUTH_FAILED", "runtime capability token is malformed")
    remainder = token[len(TOKEN_PREFIX) :]
    parts = remainder.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise PlatformError("AUTH_FAILED", "runtime capability token is malformed")
    encoded_payload, signature = parts[0], parts[1]
    expected = _sign(encoded_payload, signing_key)
    if not hmac.compare_digest(signature, expected):
        raise PlatformError("AUTH_FAILED", "runtime capability signature is invalid")
    try:
        payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise PlatformError("AUTH_FAILED", "runtime capability payload is invalid") from error
    if not isinstance(payload, dict):
        raise PlatformError("AUTH_FAILED", "runtime capability payload is invalid")
    version = payload.get("v")
    subject = payload.get("sub")
    issued_at_raw = payload.get("iat")
    expires_at_raw = payload.get("exp")
    if (
        version != _PAYLOAD_VERSION
        or not isinstance(subject, str)
        or not isinstance(issued_at_raw, int)
        or not isinstance(expires_at_raw, int)
    ):
        raise PlatformError("AUTH_FAILED", "runtime capability payload is invalid")
    if subject != attempt_id:
        raise PlatformError(
            "AUTH_FAILED", "runtime capability subject does not match the attempt"
        )
    issued_at = datetime.fromtimestamp(issued_at_raw, tz=UTC)
    expires_at = datetime.fromtimestamp(expires_at_raw, tz=UTC)
    current = now or datetime.now(UTC)
    if current < issued_at - timedelta(seconds=_LEEWAY_SECONDS):
        raise PlatformError("AUTH_FAILED", "runtime capability is not yet valid")
    if current > expires_at + timedelta(seconds=_LEEWAY_SECONDS):
        raise PlatformError("AUTH_EXPIRED", "runtime capability has expired")
    return RuntimeTokenClaims(
        attempt_id=subject,
        issued_at=issued_at,
        expires_at=expires_at,
    )


__all__ = [
    "CAPABILITY_KEY_ENV",
    "DEFAULT_RUNTIME_TOKEN_TTL_SECONDS",
    "DEMO_CAPABILITY_KEY",
    "RuntimeTokenClaims",
    "issue_runtime_token",
    "resolve_capability_key",
    "verify_runtime_token",
]
