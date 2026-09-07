"""Incident review closure (SDD §3.4): approve / reject → rerun round trips.

Drives the deterministic IncidentReviewHarness — the same durable services the
public ``POST /v1/runs/{id}/actions`` route and the ApprovalCard UI consume —
and asserts the business loop end to end:

  * run → investigate (real incident payloads) → report artifact → approval
    pause → reviewer decides through a surface-bound UiActionCommand;
  * approve ⇒ exactly one durable Effect executes (simulated ITSM write) and
    the successor Attempt finishes the Run SUCCEEDED;
  * reject ⇒ proposal consumed + Run RECOVERING; round 2 resumes from the same
    Checkpoint, publishes a revised report and re-pauses (SDD rerun semantics);
  * stale / mismatched / unbound action commands are rejected (digest identity).
"""
from __future__ import annotations

import asyncio

import pytest

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ApprovalState,
    EffectState,
    RunState,
)
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.reference.incident_closure import (
    IncidentPausedRun,
    IncidentReviewHarness,
)

REVIEWER = "oncall-reviewer"


async def _approve_ready(seed: str) -> tuple[IncidentReviewHarness, IncidentPausedRun]:
    harness = IncidentReviewHarness(idempotency_seed=seed)
    active, investigation = await harness.start_and_investigate()
    paused = await harness.pause_at_review(active, investigation, round_number=1)
    return harness, paused


def test_incident_approve_closure() -> None:
    async def scenario() -> None:
        harness = IncidentReviewHarness(idempotency_seed="approve")
        active, investigation = await harness.start_and_investigate()
        assert investigation.ticket["ticket_id"] == "T20260907"
        assert investigation.report["root_cause_candidate"] == (
            "db slow query on pay-service order index"
        )

        paused = await harness.pause_at_review(active, investigation, round_number=1)
        # Run is parked at the ApprovalCard with an OPEN proposal.
        assert paused.run.status is RunState.WAITING_APPROVAL
        assert paused.approval_surface.document["component"] == "ApprovalCard"
        props = paused.approval_surface.document["props"]
        assert props["approve_key"] == f"approval:{paused.approval.approval_id}:approve"
        assert props["reject_key"] == f"approval:{paused.approval.approval_id}:reject"
        assert props["displayed_digest"] == paused.approval.request_digest

        completed = await harness.approve_and_complete(
            paused,
            reviewer=REVIEWER,
            client_action_id="incident-approve-1",
        )
        assert completed.run.status is RunState.SUCCEEDED
        assert completed.effect.state is EffectState.SUCCEEDED
        assert completed.approval.status is ApprovalState.APPROVED
        # The simulated external ITSM write happened once, keyed by the Effect.
        assert completed.external_record.target == "project:reference"
        assert completed.external_record.effect_key == completed.effect.effect_key

    asyncio.run(scenario())


def test_incident_reject_then_rerun_approve_closure() -> None:
    async def scenario() -> None:
        harness = IncidentReviewHarness(idempotency_seed="reject")
        active, investigation = await harness.start_and_investigate()
        paused1 = await harness.pause_at_review(active, investigation, round_number=1)

        # Reviewer rejects the first report.
        run = await harness.reject(
            paused1,
            reviewer=REVIEWER,
            client_action_id="incident-reject-1",
        )
        assert run.status is RunState.RECOVERING
        rejected = await harness.store.get_action_proposal(
            harness.tenant_id, paused1.action_proposal.action_ref
        )
        assert rejected.status is ActionProposalState.REJECTED

        # Round 2 resumes from the same Checkpoint with a revised report.
        active2, revised = await harness.resume_round(paused1)
        assert revised.report["revision"] == 2
        assert active2.checkpoint.checkpoint_id == paused1.checkpoint.checkpoint_id
        assert active2.attempt.attempt_id != paused1.attempt.attempt_id

        paused2 = await harness.pause_at_review(active2, revised, round_number=2)
        assert paused2.round_number == 2
        # A second (still pending) proposal was created for round 2.
        proposal2 = await harness.store.get_action_proposal(
            harness.tenant_id, paused2.action_proposal.action_ref
        )
        assert proposal2.status is ActionProposalState.OPEN
        assert proposal2.action_ref != paused1.action_proposal.action_ref

        completed = await harness.approve_and_complete(
            paused2,
            reviewer=REVIEWER,
            client_action_id="incident-approve-2",
        )
        assert completed.run.status is RunState.SUCCEEDED
        # Exactly one Effect exists for the whole run: round 1 was never approved.
        effects = await harness.store.list_effects(harness.tenant_id, completed.run.run_id)
        assert len(effects) == 1
        assert effects[0].effect_id == completed.effect.effect_id

    asyncio.run(scenario())


def test_incident_wrong_digest_or_unbound_action_rejected() -> None:
    async def scenario() -> None:
        harness, paused = await _approve_ready("digest")
        # Decision with a tampered displayed digest is refused.
        command = harness._decision_command(
            paused, decision="APPROVE", client_action_id="incident-tamper-1"
        )
        command = command.model_copy(update={"displayed_digest": "sha256:forged"})
        with pytest.raises(PlatformError) as exc:
            await harness.ui_actions.handle(
                harness._decision_context(paused, REVIEWER),
                command,
                idempotency_key="incident-tamper-1",
            )
        assert exc.value.code == "APPROVAL_DECISION_REJECTED"

        # An action_ref not bound to the surface revision is refused.
        command2 = harness._decision_command(
            paused, decision="APPROVE", client_action_id="incident-unbound-1"
        )
        command2 = command2.model_copy(
            update={
                "surface_id": "approval-does-not-exist",
                "action_ref": f"approval:{paused.approval.approval_id}:approve",
            }
        )
        with pytest.raises(PlatformError) as exc2:
            await harness.ui_actions.handle(
                harness._decision_context(paused, REVIEWER),
                command2,
                idempotency_key="incident-unbound-1",
            )
        assert exc2.value.code in {"SURFACE_MISMATCH", "SURFACE_INVALID", "NOT_FOUND"}
        # Approval is still pending (nothing was consumed by the failed attempts).
        approval = await harness.store.get_approval_request(
            harness.tenant_id, paused.approval.approval_id
        )
        assert approval.status is ApprovalState.PENDING

    asyncio.run(scenario())


def test_incident_decision_idempotent_replay() -> None:
    async def scenario() -> None:
        harness, paused = await _approve_ready("idem")
        completed = await harness.approve_and_complete(
            paused,
            reviewer=REVIEWER,
            client_action_id="incident-idem-1",
        )
        assert completed.run.status is RunState.SUCCEEDED
        # Replaying the same UiActionCommand idempotency-key is a no-op (not an
        # error) and never prepares a second Effect.
        await harness.ui_actions.handle(
            harness._decision_context(paused, REVIEWER),
            harness._decision_command(
                paused, decision="APPROVE", client_action_id="incident-idem-1"
            ),
            idempotency_key="incident-idem-1",
        )
        effects = await harness.store.list_effects(harness.tenant_id, completed.run.run_id)
        assert len(effects) == 1

    asyncio.run(scenario())
