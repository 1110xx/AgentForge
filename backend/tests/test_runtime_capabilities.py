"""HMAC-SHA256 signed Runtime capability token tests (security/runtime_tokens.py).

Production-prerequisite Step 1 (docs/phase-4.5-security-decisions.md §2.3):
the child Runtime identity is no longer the deterministic plaintext
``runtime-token:{attempt_id}`` — it is an ``rt.v1.*`` HMAC-SHA256 signed
capability with signature + iat/exp + subject (attempt_id) binding, keyed by
the SecretStore-injected ``AGENT_PLATFORM_CAPABILITY_KEY``.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.security.runtime_tokens import (
    CAPABILITY_KEY_ENV,
    DEMO_CAPABILITY_KEY,
    TOKEN_PREFIX,
    issue_runtime_token,
    resolve_capability_key,
    verify_runtime_token,
)

KEY_A = "test-capability-key-a" * 4
KEY_B = "test-capability-key-b" * 4


def _payload_of(token: str) -> dict:
    assert token.startswith(TOKEN_PREFIX)
    encoded = token[len(TOKEN_PREFIX) :].split(".", 1)[0]
    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))


def test_issue_verify_round_trip_binds_subject_and_window() -> None:
    fixed = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
    token = issue_runtime_token(
        "attempt-abc", key=KEY_A, ttl_seconds=300, now=fixed
    )
    assert token.startswith("rt.v1.")
    claims = _payload_of(token)
    assert claims["v"] == 1
    assert claims["sub"] == "attempt-abc"
    assert claims["iat"] == int(fixed.timestamp())
    assert claims["exp"] == int(fixed.timestamp()) + 300

    verified = verify_runtime_token(
        token,
        attempt_id="attempt-abc",
        key=KEY_A,
        now=fixed + timedelta(seconds=120),
    )
    assert verified.attempt_id == "attempt-abc"
    assert verified.issued_at == fixed
    assert verified.expires_at == fixed + timedelta(seconds=300)


def test_wrong_key_is_rejected() -> None:
    token = issue_runtime_token("attempt-abc", key=KEY_A)
    with pytest.raises(PlatformError) as error:
        verify_runtime_token(token, attempt_id="attempt-abc", key=KEY_B)
    assert error.value.code == "AUTH_FAILED"


def test_tampered_signature_is_rejected() -> None:
    token = issue_runtime_token("attempt-abc", key=KEY_A)
    forged = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(PlatformError) as error:
        verify_runtime_token(forged, attempt_id="attempt-abc", key=KEY_A)
    assert error.value.code == "AUTH_FAILED"


def test_subject_mismatch_is_rejected() -> None:
    token = issue_runtime_token("attempt-abc", key=KEY_A)
    with pytest.raises(PlatformError) as error:
        verify_runtime_token(token, attempt_id="attempt-other", key=KEY_A)
    assert error.value.code == "AUTH_FAILED"


def test_expired_token_is_rejected() -> None:
    issued = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
    token = issue_runtime_token(
        "attempt-abc", key=KEY_A, ttl_seconds=30, now=issued
    )
    with pytest.raises(PlatformError) as error:
        verify_runtime_token(
            token,
            attempt_id="attempt-abc",
            key=KEY_A,
            now=issued + timedelta(seconds=3600),
        )
    assert error.value.code == "AUTH_EXPIRED"


def test_future_token_before_iat_leeway_is_rejected() -> None:
    issued = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
    token = issue_runtime_token("attempt-abc", key=KEY_A, now=issued)
    with pytest.raises(PlatformError) as error:
        verify_runtime_token(
            token,
            attempt_id="attempt-abc",
            key=KEY_A,
            now=issued - timedelta(seconds=3600),
        )
    assert error.value.code == "AUTH_FAILED"


def test_malformed_tokens_are_rejected() -> None:
    for malformed in (
        "",
        "plaintext-runtime-token:attempt-abc",
        "runtime-token:attempt-abc",
        "rt.v1.",
        "rt.v1.not-base64.sig",
        "rt.v1.onlyone",
        "rt.v1.a.b.extra",
        f"rt.v1.{_b64('not-json')}.{'0' * 64}",
        "rt.v1." + "x" * 4096 + "." + "0" * 64,
    ):
        with pytest.raises(PlatformError) as error:
            verify_runtime_token(malformed, attempt_id="attempt-abc", key=KEY_A)
        assert error.value.code == "AUTH_FAILED", malformed


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_legacy_plaintext_derivation_is_no_longer_accepted() -> None:
    # The whole point of the HMAC change: a plaintext runtime-token:{attempt_id}
    # bearer (previously valid) must now be rejected as malformed/forged.
    with pytest.raises(PlatformError) as error:
        verify_runtime_token(
            "runtime-token:attempt-abc", attempt_id="attempt-abc", key=KEY_A
        )
    assert error.value.code == "AUTH_FAILED"


def test_demo_fallback_key_is_deterministic_without_env(monkeypatch) -> None:
    monkeypatch.delenv(CAPABILITY_KEY_ENV, raising=False)
    assert resolve_capability_key() == DEMO_CAPABILITY_KEY
    token = issue_runtime_token("attempt-abc")
    verified = verify_runtime_token(token, attempt_id="attempt-abc")
    assert verified.attempt_id == "attempt-abc"


def test_env_key_override_is_honored(monkeypatch) -> None:
    monkeypatch.setenv(CAPABILITY_KEY_ENV, KEY_A)
    assert resolve_capability_key() == KEY_A
    # An explicitly different key (simulating another replica with a stale key)
    # must not verify a token signed under the env key.
    token = issue_runtime_token("attempt-abc", key=KEY_A)
    with pytest.raises(PlatformError):
        verify_runtime_token(token, attempt_id="attempt-abc", key=KEY_B)


def test_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError):
        issue_runtime_token("attempt-abc", key=KEY_A, ttl_seconds=0)


def test_subprocess_form_bootstrap_issues_verifiable_signed_token() -> None:
    """Local subprocess form (subprocess_orchestrator._op_bootstrap).

    The pipe path issues the same rt.v1.* signed capability at bootstrap that
    the HTTP form issues — a child Runner's bootstrap grant must verify under
    the same key the API-side verifier uses (three-form consistency).
    """
    import asyncio

    from enterprise_agent_platform.contracts.commands import CreateRunCommand
    from enterprise_agent_platform.control.context import RequestContext
    from enterprise_agent_platform.control.service import ControlPlaneService
    from enterprise_agent_platform.domain.records import DispatchTicket
    from enterprise_agent_platform.execution.pipe_transport import OP_BOOTSTRAP
    from enterprise_agent_platform.execution.subprocess_orchestrator import (
        SubprocessOrchestrator,
    )
    from enterprise_agent_platform.persistence import InMemoryPlatformStore
    from enterprise_agent_platform.security.runtime_tokens import (
        DEMO_CAPABILITY_KEY,
        verify_runtime_token,
    )

    async def _scenario() -> None:
        store = InMemoryPlatformStore()
        control = ControlPlaneService(store)
        ctx = RequestContext(
            tenant_id="tenant-pipe",
            actor_id="scheduler-test",
            scopes=("runs:create", "runs:execute"),
            request_id="req-pipe",
        )
        run = await control.create_run(
            ctx,
            CreateRunCommand(
                workflow_type="synthetic-analysis",
                intent="bootstrap token sanity",
                resource_refs=("synthetic-case:case-1",),
            ),
            idempotency_key="pipe-bootstrap-1",
        )
        unit = await store.get_primary_unit(ctx.tenant_id, run.run_id)
        checkpoint = await store.get_checkpoint(
            ctx.tenant_id, unit.current_checkpoint_id or ""
        )
        reservation = await control.reserve_attempt(
            ctx,
            unit.execution_unit_id,
            checkpoint.checkpoint_id,
            unit.version,
            transition_key="pipe-bootstrap-reserve-1",
        )
        attempt = reservation.attempt
        orchestrator = SubprocessOrchestrator(store=store, control=control)
        ticket = DispatchTicket(
            worker_id=f"subprocess:{attempt.attempt_id}",
            tenant_id=ctx.tenant_id,
            run_id=attempt.run_id,
            execution_unit_id=attempt.execution_unit_id,
            attempt_id=attempt.attempt_id,
            lease_id="lease-pipe",
            generation=attempt.generation,
            source_checkpoint_id=unit.current_checkpoint_id or "",
        )
        result = await orchestrator._handle(
            ticket,
            ctx,
            OP_BOOTSTRAP,
            {"attempt_id": attempt.attempt_id, "generation": attempt.generation},
        )
        token = result["runtime_token"]
        assert token.startswith("rt.v1."), token
        # Demo fallback key (no env in tests) is shared by the HTTP verifier,
        # so the same token would verify across the internal API.
        verified = verify_runtime_token(
            token, attempt_id=attempt.attempt_id, key=DEMO_CAPABILITY_KEY
        )
        assert verified.attempt_id == attempt.attempt_id
        assert result["lease_version"] >= 2, "bootstrap must activate the Lease"

    asyncio.run(_scenario())
