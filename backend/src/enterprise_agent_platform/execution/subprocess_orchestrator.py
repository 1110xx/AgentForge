"""Parent-side SubprocessOrchestrator for the Phase-1 local execution chain.

One child Python process per Attempt, communicating with the parent over a
JSON-line pipe (stdin/stdout). This mirrors the production Scheduler →
Orchestrator → Runtime(Pod) shape without Docker or cluster I/O::

    Scheduler ── claim_ready_work() → DispatchTicket
        ↓
    SubprocessOrchestrator.execute(ticket)
        ├─ spawn: python -m ...subprocess_runtime
        ├─ pipe:  child request ops → parent handlers
        │    bootstrap → activate_lease + grant identity
        │    restore   → checkpoint with run intent/resource refs
        │    heartbeat → renew_lease
        │    read_tool → resource proxy (demo resolver)
        │    model_call→ parent's RunSessionProvider (DeepSeek)
        │    commit_final_checkpoint / record_failure → terminal transition
        └─ child exit → destroy process, close session
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ArtifactVersionState,
    AttemptState,
    EntityType,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.contracts.events import (
    ActionProposalPayload,
    ArtifactVersionPayload,
    EnterpriseEventEnvelope,
)
from enterprise_agent_platform.control.checkpoints import CheckpointCommit, commit_checkpoint
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.action_digest import compute_action_request_digest
from enterprise_agent_platform.domain.fsm import transition as _fsm
from enterprise_agent_platform.domain.records import (
    ActionProposalRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    DispatchTicket,
    OutboxMessageRecord,
    RunRecord,
)
from enterprise_agent_platform.execution.completer import RunCompleter
from enterprise_agent_platform.execution.pipe_transport import (
    OP_BOOTSTRAP,
    OP_COMMIT_CHECKPOINT,
    OP_COMMIT_FINAL,
    OP_HEARTBEAT,
    OP_MODEL_CALL,
    OP_PROPOSE_ACTION,
    OP_PUBLISH_ARTIFACT,
    OP_READ_TOOL,
    OP_RECORD_FAILURE,
    OP_RESTORE,
    error_response,
    ok_response,
)
from enterprise_agent_platform.execution.session import SessionHandle
from enterprise_agent_platform.persistence.protocol import (
    PlatformError,
    PlatformStore,
)

logger = logging.getLogger(__name__)

_SRC_ROOT = Path(__file__).resolve().parent.parent.parent  # .../backend/src


class SubprocessOrchestrator:
    """Spawns and supervises one child runtime process per claimed Attempt."""

    def __init__(
        self,
        store: PlatformStore,
        control: ControlPlaneService,
        run_sessions=None,
        resource_resolver=None,
        python: str | None = None,
        max_runtime_seconds: float = 120.0,
    ) -> None:
        self._store = store
        self._control = control
        self._run_sessions = run_sessions
        self._resource_resolver = resource_resolver
        self._python = python or sys.executable
        self._max_runtime_seconds = max_runtime_seconds
        self._max_retries = 2  # max crash-auto-retry count per Attempt
        self._completer = RunCompleter(store)
        # One model session handle per Run (children are destroyed per attempt,
        # but the parent-side session provider is long-lived).
        self._sessions: dict[str, SessionHandle] = {}

    # ── Public entry ──────────────────────────────────────────────────────

    async def execute(self, ticket: DispatchTicket) -> None:
        """Run one Attempt end-to-end in a child process."""
        ctx = self._runtime_context(ticket)
        logger.info(
            "SubprocessOrchestrator dispatching: run=%s attempt=%s gen=%d",
            ticket.run_id,
            ticket.attempt_id,
            ticket.generation,
        )
        env = self._child_env(ticket)
        try:
            process = await self._spawn(env)
        except Exception as error:
            logger.exception("spawn failed for run=%s", ticket.run_id)
            await self._fail(ticket, f"spawn failed: {error}")
            return

        stderr_task = asyncio.create_task(self._drain_stderr(process))

        rc: int | None = None
        try:
            rc = await self._serve(process, ticket, ctx)
        except TimeoutError:
            logger.error("runtime timeout for run=%s — killing child", ticket.run_id)
            process.kill()
            await process.wait()
            await self._fail(ticket, f"runtime exceeded {self._max_runtime_seconds}s")
            rc = None
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            await self._close_session(ticket.run_id)

        if rc is not None and rc != 0:
            logger.warning(
                "child exited rc=%d for run=%s — recording failure",
                rc,
                ticket.run_id,
            )
            await self._fail(ticket, f"child runtime exited with code {rc}")

    # ── Child lifecycle ───────────────────────────────────────────────────

    def _child_env(self, ticket: DispatchTicket) -> dict[str, str]:
        env = dict(os.environ)
        src_paths = [str(_SRC_ROOT)]
        if env.get("PYTHONPATH"):
            src_paths.insert(0, env["PYTHONPATH"])
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(src_paths),
                "PYTHONUNBUFFERED": "1",
                "AGENT_PLATFORM_ATTEMPT_ID": ticket.attempt_id,
                "AGENT_PLATFORM_GENERATION": str(ticket.generation),
                "AGENT_PLATFORM_BOOTSTRAP_TOKEN": f"bootstrap:{ticket.attempt_id}",
                "AGENT_PLATFORM_POD_UID": f"subprocess:{ticket.attempt_id}",
            }
        )
        return env

    async def _spawn(self, env: dict[str, str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self._python,
            "-u",
            "-m",
            "enterprise_agent_platform.execution.subprocess_runtime",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        while True:
            assert process.stderr is not None
            raw = await process.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.info("runtime-child: %s", line)

    # ── Pipe service loop ─────────────────────────────────────────────────

    async def _serve(
        self,
        process: asyncio.subprocess.Process,
        ticket: DispatchTicket,
        ctx: RequestContext,
    ) -> int:
        async def _request_loop() -> None:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                try:
                    request = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    logger.warning("ignoring malformed child line: %r", raw[:120])
                    continue
                request_id = request.get("id")
                op = request.get("op")
                kwargs = request.get("kwargs") or {}
                asyncio.create_task(
                    self._respond(process, ticket, ctx, request_id, op, kwargs)
                )

        reader_task = asyncio.create_task(_request_loop())
        try:
            async with asyncio.timeout(self._max_runtime_seconds):
                return_code = await process.wait()
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
        return return_code

    async def _respond(
        self,
        process: asyncio.subprocess.Process,
        ticket: DispatchTicket,
        ctx: RequestContext,
        request_id: int,
        op: str,
        kwargs: dict[str, Any],
    ) -> None:
        try:
            result = await self._handle(ticket, ctx, op, kwargs)
            response = ok_response(request_id, result)
        except PlatformError as error:
            logger.warning("runtime op %s failed: %s", op, error)
            response = error_response(request_id, error.code, error.message)
        except Exception as error:
            logger.exception("runtime op %s crashed", op)
            response = error_response(request_id, "INTERNAL_ERROR", str(error))
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The child exited before reading the response — nothing to do.
            logger.debug("child pipe closed before response for op %s", op)

    # ── Operation handlers ────────────────────────────────────────────────

    async def _handle(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        op: str,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if op == OP_BOOTSTRAP:
            return await self._op_bootstrap(ticket, ctx, kwargs)
        if op == OP_RESTORE:
            return await self._op_restore(ticket, kwargs)
        if op == OP_HEARTBEAT:
            return await self._op_heartbeat(ctx, kwargs)
        if op == OP_READ_TOOL:
            return await self._op_read_tool(ctx, kwargs)
        if op == OP_MODEL_CALL:
            return await self._op_model_call(ticket, kwargs)
        if op == OP_PUBLISH_ARTIFACT:
            return await self._op_publish_artifact(ticket, ctx, kwargs)
        if op == OP_PROPOSE_ACTION:
            return await self._op_propose_action(ticket, ctx, kwargs)
        if op == OP_COMMIT_CHECKPOINT:
            agent_state = kwargs.get("agent_state") or {}
            schema_version = str(
                kwargs.get("agent_state_schema_version") or "pi-agent-core/v1"
            )
            checkpoint = await self._commit_runtime_checkpoint(
                ticket,
                ctx,
                kwargs,
                agent_state=agent_state,
                agent_state_schema_version=schema_version,
            )
            return {
                "status": "committed",
                "checkpoint_id": checkpoint.checkpoint_id,
            }
        if op == OP_COMMIT_FINAL:
            summary = str(kwargs.get("summary", "")) or "Completed."
            agent_state = kwargs.get("agent_state") or {}
            schema_version = str(
                kwargs.get("agent_state_schema_version") or "pi-agent-core/v1"
            )
            # Persist the final Agent snapshot first so a follow-up / rerun
            # Attempt can rehydrate conversation history, then terminalize.
            checkpoint = await self._commit_runtime_checkpoint(
                ticket,
                ctx,
                kwargs,
                agent_state=agent_state,
                agent_state_schema_version=schema_version,
                summary=summary,
            )
            run = await self._store.get_run(ticket.tenant_id, ticket.run_id)
            await self._completer.complete_run(ctx, ticket, run)
            answered = await self._answer_pending_followup(ticket, summary)
            return {
                "status": "committed",
                "followup_answered": answered,
                "checkpoint_id": checkpoint.checkpoint_id,
            }
        if op == OP_RECORD_FAILURE:
            run = await self._store.get_run(ticket.tenant_id, ticket.run_id)
            reason = str(kwargs.get("reason_code", "RUNTIME_FAILURE"))
            await self._retry_or_fail(
                ticket, ctx, run, RuntimeError(f"child reported {reason}")
            )
            return {"status": "recorded"}
        raise PlatformError("UNKNOWN_OPERATION", f"runtime op not supported: {op}")

    async def _op_bootstrap(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        attempt_id = str(kwargs.get("attempt_id", ticket.attempt_id))
        generation = int(kwargs.get("generation", ticket.generation))
        owner = f"subprocess-runtime:{attempt_id}"
        lease = await self._control.activate_lease(
            ctx,
            attempt_id,
            generation,
            owner=owner,
            expected_lease_version=1,
        )
        return {
            "runtime_token": f"runtime-token:{attempt_id}",
            "lease_owner": owner,
            "lease_version": lease.version,
            "expires_at": lease.expires_at.isoformat() if lease.expires_at else "",
        }

    async def _commit_runtime_checkpoint(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        kwargs: dict[str, Any],
        *,
        agent_state: dict[str, Any],
        agent_state_schema_version: str,
        summary: str | None = None,
    ) -> object:
        """Persist a pi-agent-core Agent snapshot as the next committed Checkpoint.

        Mirrors the Runner contract: the Attempt is fenced into CHECKPOINTING
        (CAS against its current status) before ``control.checkpoints.
        commit_checkpoint`` validates the full runtime facts (generation fence,
        Lease ownership/version, source cursor chain) and writes a new
        Checkpoint with ``checkpoint_seq + 1``. The source cursor is always the
        unit's current committed checkpoint, so the chain advances across turns
        without the child having to track checkpoint ids.
        """
        from dataclasses import replace as _replace

        context_kwargs = kwargs.get("context") or {}
        lease_owner = str(context_kwargs.get("lease_owner", ""))
        expected_lease_version = int(context_kwargs.get("lease_version", 0))

        async with self._store.transaction() as tx:
            now = await tx.db_now()
            current = await tx.get_attempt(ticket.tenant_id, ticket.attempt_id)
            if current.status is AttemptState.CLAIMED:
                _fsm(EntityType.ATTEMPT, current.status, AttemptState.RUNNING, None)
                running = _replace(
                    current,
                    status=AttemptState.RUNNING,
                    version=current.version + 1,
                    updated_at=now,
                )
                await tx.replace_attempt_cas(running, current.version)
                _fsm(EntityType.ATTEMPT, running.status, AttemptState.CHECKPOINTING, None)
                checkpointing = _replace(
                    running,
                    status=AttemptState.CHECKPOINTING,
                    version=running.version + 1,
                    updated_at=now,
                )
                await tx.replace_attempt_cas(checkpointing, running.version)
            elif current.status is AttemptState.RUNNING:
                _fsm(EntityType.ATTEMPT, current.status, AttemptState.CHECKPOINTING, None)
                checkpointing = _replace(
                    current,
                    status=AttemptState.CHECKPOINTING,
                    version=current.version + 1,
                    updated_at=now,
                )
                await tx.replace_attempt_cas(checkpointing, current.version)
            else:
                raise PlatformError(
                    "INVALID_STATE",
                    f"attempt cannot start checkpointing from {current.status.value}",
                )

        run = await self._store.get_run(ticket.tenant_id, ticket.run_id)
        cursor: dict[str, Any] = {
            "intent": run.intent,
            "resource_refs": list(run.resource_refs),
        }
        if summary is not None:
            cursor["summary"] = summary
        checksum = hashlib.sha256(
            json.dumps(
                agent_state,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        unit = await self._store.get_execution_unit(
            ticket.tenant_id, ticket.execution_unit_id
        )
        checkpoint = await commit_checkpoint(
            self._store,
            ctx,
            attempt_id=ticket.attempt_id,
            generation=ticket.generation,
            lease_owner=lease_owner,
            expected_lease_version=expected_lease_version,
            command=CheckpointCommit(
                source_checkpoint_id=(
                    unit.current_checkpoint_id or ticket.source_checkpoint_id
                ),
                workflow_cursor=cursor,
                checksum=checksum,
                agent_state=agent_state,
                agent_state_schema_version=agent_state_schema_version or "pi-agent-core/v1",
            ),
        )
        return checkpoint

    async def _op_restore(
        self,
        ticket: DispatchTicket,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        attempt = await self._store.get_attempt(
            ticket.tenant_id, ticket.attempt_id
        )
        run = await self._store.get_run(ticket.tenant_id, attempt.run_id)
        # Load the actual committed Checkpoint so the child Runner can
        # rehydrate the pi-agent-core Agent from its persisted snapshot
        # (``agent_state``) instead of starting from a blank Agent.
        checkpoint = await self._store.get_checkpoint(
            ticket.tenant_id, ticket.source_checkpoint_id
        )
        run_cursor = {
            "run_id": run.run_id,
            "workflow_type": run.workflow_type,
            "intent": run.intent,
            "resource_refs": list(run.resource_refs),
        }
        # Persisted cursor (per-run progress) is merged under authoritative
        # Run facts: the current intent/resource_refs always win.
        cursor = dict(checkpoint.workflow_cursor)
        cursor.update(run_cursor)
        # Follow-up reactivation: if a PENDING follow-up is queued for this
        # Run, inject the question into the restore cursor so the fresh child
        # Runner answers ``intent + question`` instead of re-running the intent.
        followups = await self._store.list_followup_requests(
            ticket.tenant_id, run.run_id
        )
        pending = [f for f in followups if f.status == "PENDING"]
        if pending:
            cursor["followup_id"] = pending[0].followup_id
            cursor["followup_question"] = pending[0].question
        return {
            "checkpoint_id": ticket.source_checkpoint_id,
            "checkpoint_state": "COMMITTED",
            "snapshot_state": None,
            "workflow_cursor": cursor,
            "agent_state": checkpoint.agent_state or {},
            "agent_state_schema_version": checkpoint.agent_state_schema_version,
        }

    async def _answer_pending_followup(
        self,
        ticket: DispatchTicket,
        summary: str,
    ) -> bool:
        """Mark the oldest PENDING follow-up of this Run as ANSWERED.

        Called on the commit_final path: the child Runner's commit summary is
        the answer for the follow-up question that was injected at restore.
        """
        followups = await self._store.list_followup_requests(
            ticket.tenant_id, ticket.run_id
        )
        pending = [f for f in followups if f.status == "PENDING"]
        if not pending:
            return False
        oldest = pending[0]
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            current = await tx.get_followup_request(
                ticket.tenant_id, oldest.followup_id
            )
            if current.status != "PENDING" or current.version != oldest.version:
                return False
            answered = replace(
                current,
                status="ANSWERED",
                answer=summary,
                version=current.version + 1,
                answered_at=now,
            )
            await tx.replace_followup_request_cas(answered, current.version)
            return True

    async def _op_heartbeat(
        self,
        ctx: RequestContext,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        context = kwargs.get("context") or {}
        attempt_id = str(context["attempt_id"])
        generation = int(context["generation"])
        lease = await self._control.renew_lease(
            ctx,
            attempt_id,
            generation,
            owner=str(context["lease_owner"]),
            expected_lease_version=int(context["lease_version"]),
        )
        return {
            "attempt_id": attempt_id,
            "generation": generation,
            "pod_uid": str(context.get("pod_uid", "")),
            "runtime_token": str(context.get("runtime_token", "")),
            "lease_owner": str(context["lease_owner"]),
            "lease_version": lease.version,
        }

    async def _op_read_tool(
        self,
        ctx: RequestContext,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        resource_ref = str(kwargs.get("arguments_ref", ""))
        if self._resource_resolver is None:
            return {
                "tool_name": kwargs.get("tool_name", ""),
                "resource_ref": resource_ref,
                "resolved": None,
                "content": f"[demo] proxied read of {resource_ref} (no resolver)",
            }
        resolved = await self._resource_resolver.resolve(ctx, resource_ref)
        return {
            "tool_name": kwargs.get("tool_name", ""),
            "resource_ref": resource_ref,
            "resolved": {
                "resource_ref": resolved.resource_ref,
                "canonical_id": resolved.canonical_id,
                "classification": resolved.classification,
                "version": resolved.version,
                "digest": resolved.digest,
            },
            "content": (
                f"[demo] resource {resource_ref} resolved through proxy "
                f"({resolved.classification} v{resolved.version})"
            ),
        }

    async def _op_publish_artifact(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Store an artifact metadata record emitted by the child runtime.

        The child stages the artifact content in its local workspace; the parent
        persists the metadata (logical name, classification, etc.) and creates
        a "pending" artifact version. The actual content upload happens at
        commit_final time when the workspace snapshot is taken.
        """
        workspace_path = str(kwargs.get("workspace_path", ""))
        logical_name = str(kwargs.get("logical_name", workspace_path.split("/")[-1]))
        classification = str(kwargs.get("classification", "general"))

        now = datetime.now(UTC)
        artifact_id = self._store.new_id("artifact")

        async with self._store.transaction() as tx:
            # Lock the run to derive the event sequence and guard terminal state.
            run = await tx.lock_run(ticket.tenant_id, ticket.run_id)
            if run.status not in {RunState.QUEUED, RunState.RUNNING}:
                logger.warning(
                    "publish_artifact skipped: run=%s not RUNNING (status=%s)",
                    run.run_id, run.status,
                )
                return {"status": "rejected", "reason": "run not RUNNING"}

            # Insert artifact master record
            artifact = ArtifactRecord(
                tenant_id=ticket.tenant_id,
                artifact_id=artifact_id,
                run_id=run.run_id,
                logical_name=logical_name,
                artifact_type=classification,
                classification=classification,
                retention_policy={"policy": "default"},
                # Master is a directory entry: ACTIVE from birth (content readiness
                # lives on the version row; SQL CHECK allows only ACTIVE/DELETED).
                state="ACTIVE",
                current_version=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            await tx.insert_artifact(artifact)

            # Insert version record (PREPARING — finalized at commit)
            version_record = ArtifactVersionRecord(
                tenant_id=ticket.tenant_id,
                artifact_id=artifact_id,
                version=1,
                run_id=run.run_id,
                source_attempt_id=ticket.attempt_id,
                generation=ticket.generation,
                state=ArtifactVersionState.STAGING,
                state_version=1,
                object_uri="staged:" + workspace_path,
                checksum="",
                size_bytes=0,
                media_type="application/octet-stream",
                lineage={"run_id": run.run_id, "attempt_id": ticket.attempt_id},
                created_at=now,
                ready_at=None,
            )
            await tx.insert_artifact_version(version_record)

            # Emit ARTIFACT_VERSION event + outbox so subscribers see the
            # staged version (finalized at commit time, per runner contract).
            artifact_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ARTIFACT_VERSION,
                occurred_at=now,
                producer_service="subprocess-orchestrator",
                payload_schema="artifact-version/v1",
                payload=ArtifactVersionPayload(
                    kind="artifact.version",
                    artifact_id=artifact_id,
                    run_id=run.run_id,
                    logical_name=logical_name,
                    classification=classification,
                    version=1,
                    state="STAGING",
                ),
                attempt_id=ticket.attempt_id,
                trace_id=ctx.trace_id,
            )
            await tx.append_event(artifact_event, run.last_event_seq)
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ticket.tenant_id,
                    message_id=self._store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="artifact.prepared",
                    payload={"artifact_id": artifact_id, "version": 1},
                    event_id=artifact_event.event_id,
                    aggregate_version=run.version + 1,
                    created_at=now,
                    published_at=None,
                )
            )
            # Advance the run event watermark (mirrors checkpoint commit pattern).
            await tx.replace_run_cas(
                replace(
                    run,
                    version=run.version + 1,
                    last_event_seq=artifact_event.event_seq,
                    updated_at=now,
                ),
                run.version,
            )

        logger.info(
            "Artifact recorded: run=%s artifact=%s logical_name=%s classification=%s",
            run.run_id, artifact_id, logical_name, classification,
        )
        return {
            "status": "accepted",
            "artifact_id": artifact_id,
            "logical_name": logical_name,
            "version": 1,
        }

    async def _op_propose_action(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Store an action proposal record emitted by the child runtime.

        The proposal represents an action the agent wants to take that requires
        external approval or tracking (e.g. calling an external API, modifying
        a resource). The parent persists it with OPEN status; a human or policy
        engine can later consume or reject it.
        """
        action_ref = str(kwargs.get("action_ref", ""))
        canonical_payload_ref = str(kwargs.get("canonical_payload_ref", ""))

        if not action_ref:
            return {
                "status": "rejected",
                "reason": "action_ref is required",
            }

        now = datetime.now(UTC)
        # Authority facts: the child may provide them explicitly; otherwise the
        # proxy defaults to a canonical self-targeted action so the digest is
        # always well-defined and approval decisions stay verifiable.
        tool_name = str(kwargs.get("tool_name", "remote_propose_action"))
        tool_spec_version = str(kwargs.get("tool_spec_version", "1.0"))
        tool_spec_digest = str(kwargs.get("tool_spec_digest", "sha256:proposed"))
        connector_name = str(kwargs.get("connector_name", "control-plane-default"))
        required_scopes = tuple(sorted(set(kwargs.get("required_scopes", ("actions:execute",)))))
        canonical_target = str(kwargs.get("canonical_target", "action://" + action_ref))
        canonical_payload_digest = (
            str(kwargs.get("canonical_payload_digest"))
            or ("sha256:" + canonical_payload_ref if canonical_payload_ref else "")
        )
        risk_class = str(kwargs.get("risk_class", "unknown"))
        request_digest = compute_action_request_digest(
            action_ref=action_ref,
            tool_name=tool_name,
            tool_spec_version=tool_spec_version,
            tool_spec_digest=tool_spec_digest,
            connector_name=connector_name,
            required_scopes=required_scopes,
            canonical_target=canonical_target,
            canonical_payload_digest=canonical_payload_digest,
            risk_class=risk_class,
        )

        async with self._store.transaction() as tx:
            # Lock the run to derive the event sequence and guard terminal state.
            run = await tx.lock_run(ticket.tenant_id, ticket.run_id)
            if run.status not in {RunState.QUEUED, RunState.RUNNING}:
                logger.warning(
                    "propose_action skipped: run=%s not RUNNING (status=%s)",
                    run.run_id, run.status,
                )
                return {"status": "rejected", "reason": "run not RUNNING"}

            proposal = ActionProposalRecord(
                tenant_id=ticket.tenant_id,
                action_ref=action_ref,
                run_id=run.run_id,
                step_id=None,
                attempt_id=ticket.attempt_id,
                execution_unit_id=ticket.execution_unit_id,
                source_generation=ticket.generation,
                tool_name=tool_name,
                tool_spec_version=tool_spec_version,
                tool_spec_digest=tool_spec_digest,
                connector_name=connector_name,
                required_scopes=required_scopes,
                canonical_payload_digest=canonical_payload_digest,
                canonical_target=canonical_target,
                risk_class=risk_class,
                status=ActionProposalState.OPEN,
                version=1,
                request_digest=request_digest,
                payload_ref=canonical_payload_ref,
                created_at=now,
                expires_at=now + timedelta(days=7),
            )
            await tx.insert_action_proposal(proposal)

            # Emit ACTION_PROPOSAL event + outbox so approvers see the proposal.
            proposal_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ACTION_PROPOSAL,
                occurred_at=now,
                producer_service="subprocess-orchestrator",
                payload_schema="action-proposal/v1",
                payload=ActionProposalPayload(
                    kind="action.proposal",
                    action_ref=action_ref,
                    run_id=run.run_id,
                    attempt_id=ticket.attempt_id,
                    proposal_state="OPEN",
                    risk_class="unknown",
                ),
                attempt_id=ticket.attempt_id,
                trace_id=ctx.trace_id,
            )
            await tx.append_event(proposal_event, run.last_event_seq)
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ticket.tenant_id,
                    message_id=self._store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="action.proposed",
                    payload={"action_ref": action_ref},
                    event_id=proposal_event.event_id,
                    aggregate_version=run.version + 1,
                    created_at=now,
                    published_at=None,
                )
            )
            # Advance the run event watermark (mirrors checkpoint commit pattern).
            await tx.replace_run_cas(
                replace(
                    run,
                    version=run.version + 1,
                    last_event_seq=proposal_event.event_seq,
                    updated_at=now,
                ),
                run.version,
            )

        logger.info(
            "Action proposal recorded: run=%s action_ref=%s",
            run.run_id, action_ref,
        )
        return {
            "status": "accepted",
            "action_ref": action_ref,
            "proposal_state": "OPEN",
        }

    async def _op_model_call(
        self,
        ticket: DispatchTicket,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a full model call request from the child process.

        The request includes the full message history + tool definitions.
        The parent proxies this to the real LLM provider and returns
        a structured response with content blocks (text, tool_use).
        """
        model_info = kwargs.get("model", {})
        system_prompt = kwargs.get("system_prompt", "")
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools", [])
        options = kwargs.get("options", {})

        if self._run_sessions is None:
            # No provider configured — return a mock response
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"[no model provider] run {ticket.run_id} "
                            f"intent={kwargs.get('intent', '')}"
                        ),
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {"input": 0, "output": 0, "total_tokens": 0},
            }

        handle = self._sessions.get(ticket.run_id)
        if handle is None:
            run = await self._store.get_run(ticket.tenant_id, ticket.run_id)
            handle = await self._run_sessions.open(
                run_id=run.run_id,
                intent=run.intent,
                resource_refs=run.resource_refs,
                host_context_ref=run.host_context_ref,
            )
            self._sessions[ticket.run_id] = handle

        # Build the API request payload
        # DeepSeek expects messages in standard OpenAI-compatible format
        api_messages: list[dict[str, Any]] = []

        # System prompt
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})

        # Convert pi-agent-core messages to API format
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", [])

            if role == "user":
                # User messages: extract text from content blocks
                texts: list[str] = []
                for block in content if isinstance(content, list) else [{"text": str(content)}]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif isinstance(block, dict) and block.get("type") == "image":
                        texts.append("[image]")
                api_messages.append({"role": "user", "content": "\n".join(texts)})

            elif role == "assistant":
                assistant_content: list[dict[str, Any]] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            assistant_content.append({"type": "text", "text": block.get("text", "")})
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                msg_entry: dict[str, Any] = {"role": "assistant"}
                if assistant_content:
                    msg_entry["content"] = assistant_content
                else:
                    msg_entry["content"] = ""
                if tool_calls:
                    msg_entry["tool_calls"] = tool_calls
                api_messages.append(msg_entry)

            elif role == "tool_result":
                tool_call_id = msg.get("tool_call_id", "")
                tool_name = msg.get("tool_name", "")
                result_text = ""
                for block in content if isinstance(content, list) else [{"text": str(content)}]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        result_text = block.get("text", "")
                        break
                api_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_text,
                })

        # Try to use tool-calling API if the provider supports it
        # DeepSeek models support function calling
        if hasattr(self._run_sessions, "_call_api_tools"):
            try:
                result = await self._run_sessions._call_api_tools(
                    messages=api_messages,
                    tools=tools,
                )
                return result
            except Exception:
                # Fall back to followup text-only
                pass

        # Fallback: call followup with a single text prompt
        # Flatten messages to a single prompt string
        flat_prompt = system_prompt + "\n\n" if system_prompt else ""
        for m in api_messages:
            if isinstance(m.get("content"), str):
                flat_prompt += f"{m['role']}: {m['content']}\n\n"
        answer = await self._run_sessions.followup(handle, flat_prompt)
        return {
            "content": [{"type": "text", "text": answer}],
            "stop_reason": "end_turn",
            "usage": {"input": 0, "output": 0, "total_tokens": 0},
        }

    # ── Terminal helpers ──────────────────────────────────────────────────

    def _runtime_context(self, ticket: DispatchTicket) -> RequestContext:
        return RequestContext(
            tenant_id=ticket.tenant_id,
            actor_id=f"orchestrator:subprocess:{ticket.attempt_id}",
            scopes=(
                "runs:execute",
                "runs:read",
                "runs:write",
                "actions:execute",
                "effects:recover",
                "approvals:decide",
            ),
            request_id=f"subprocess:{ticket.run_id}:{ticket.attempt_id}",
            trace_id=f"trace:{ticket.run_id}",
        )

    async def _retry_or_fail(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        run: RunRecord,
        error: RuntimeError,
    ) -> None:
        """Decide whether to auto-retry (generation < max_retries) or terminal-fail."""
        if ticket.generation < self._max_retries:
            logger.info(
                "Auto-retry run=%s attempt=%s gen=%d/%d after: %s",
                run.run_id, ticket.attempt_id,
                ticket.generation, self._max_retries, error,
            )
            await self._transition_to_recovering(ticket, ctx, run, error)
        else:
            logger.info(
                "Max retries reached for run=%s attempt=%s gen=%d — terminal fail",
                run.run_id, ticket.attempt_id, ticket.generation,
            )
            await self._completer.fail_run(ctx, ticket, run, error)

    async def _transition_to_recovering(
        self,
        ticket: DispatchTicket,
        ctx: RequestContext,
        run: RunRecord,
        error: RuntimeError,
    ) -> None:
        """Transition Run → RECOVERING, Unit → RECOVERING, fail Attempt/Lease.

        This allows the scheduler to pick up the work again (generation+1)
        on the next polling cycle.
        """
        try:
            async with self._store.transaction() as tx:
                now = await tx.db_now()
                run = await tx.lock_run(ticket.tenant_id, run.run_id)
                unit = await tx.lock_execution_unit(
                    ticket.tenant_id, ticket.execution_unit_id
                )
                attempt = await tx.get_attempt(
                    ticket.tenant_id, ticket.attempt_id
                )
                lease = await tx.get_lease_for_attempt(
                    ticket.tenant_id, ticket.attempt_id
                )

                # ── Attempt → FAILED ──
                failed_attempt = attempt
                try:
                    _fsm(EntityType.ATTEMPT, attempt.status, AttemptState.FAILED, None)
                    failed_attempt = replace(
                        attempt,
                        status=AttemptState.FAILED,
                        version=attempt.version + 1,
                        updated_at=now,
                        ended_at=now,
                    )
                except Exception:
                    pass  # already terminal or incompatible — force update
                await tx.replace_attempt_cas(failed_attempt, attempt.version)

                # ── Lease → RELEASED ──
                released_lease = replace(
                    lease,
                    state=ExecutionLeaseState.RELEASED,
                    version=lease.version + 1,
                    released_at=now,
                    updated_at=now,
                )
                try:
                    await tx.replace_lease_cas(released_lease, lease.version)
                except Exception:
                    pass  # lease may already be expired/released

                # ── Unit → RECOVERING ──
                if unit.status is ExecutionUnitState.EXECUTING:
                    _fsm(EntityType.EXECUTION_UNIT, unit.status, ExecutionUnitState.RECOVERING, None)
                recovering_unit = replace(
                    unit,
                    status=ExecutionUnitState.RECOVERING,
                    version=unit.version + 1,
                    updated_at=now,
                )
                await tx.replace_execution_unit_cas(recovering_unit, unit.version)

                # ── Run → RECOVERING ──
                if run.status in (RunState.QUEUED, RunState.RUNNING):
                    recovering_run = replace(
                        run,
                        status=RunState.RECOVERING,
                        status_reason=str(error),
                        version=run.version + 1,
                        updated_at=now,
                    )
                    await tx.replace_run_cas(recovering_run, run.version)
                else:
                    logger.warning(
                        "Run %s in unexpected state %s for recovery",
                        run.run_id, run.status,
                    )

            logger.info(
                "Recovery transition complete: run=%s unit=%s attempt=%s",
                run.run_id, ticket.execution_unit_id, ticket.attempt_id,
            )
        except Exception:
            logger.exception(
                "Recovery transition failed for run=%s — falling back to terminal fail",
                run.run_id,
            )
            await self._completer.fail_run(ctx, ticket, run, error)

    async def _fail(self, ticket: DispatchTicket, reason: str) -> None:
        ctx = self._runtime_context(ticket)
        try:
            run = await self._store.get_run(ticket.tenant_id, ticket.run_id)
        except PlatformError:
            logger.error("run %s not found while failing", ticket.run_id)
            return
        try:
            await self._retry_or_fail(ticket, ctx, run, RuntimeError(reason))
        except Exception:
            logger.exception("retry_or_fail failed for run=%s", ticket.run_id)

    async def _close_session(self, run_id: str) -> None:
        handle = self._sessions.pop(run_id, None)
        if handle is None or self._run_sessions is None:
            return
        try:
            await self._run_sessions.close(handle)
        except Exception:
            logger.warning("failed to close session for run=%s", run_id)