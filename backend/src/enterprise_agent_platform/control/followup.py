"""Follow-up routing: terminal Runs get a fresh Attempt; live Runs answer inline.

Architecture fact (SDD §6.4): the pi-agent-core Agent instance is destroyed when
the task's Attempt finishes, so ``steer()`` / ``follow_up()`` queues cannot route
follow-ups. A follow-up on a **terminal** Run (SUCCEEDED/FAILED) is therefore:

  1. durably queued as a ``FollowupRequestRecord`` while the Run/Unit transition
     ``SUCCEEDED/FAILED -> RECOVERING``,
  2. picked up by the next Scheduler polling cycle -> a new Attempt (generation+1)
     is reserved and a fresh child Runner is spawned,
  3. injected into the child's restore cursor (``followup_question``), answered by
     a new pi-agent-core Agent whose prompt is ``intent + question``,
  4. written back to the ``FollowupRequestRecord`` as ANSWERED when the Attempt
     commits; the answer is read from the commit summary.

The HTTP endpoint polls the durable record until the answer lands, then returns
it (the REST contract stays synchronous).

For Runs that are still QUEUED/RUNNING/RECOVERING (no finished Attempt yet, no
Agent has been destroyed) the service falls back to the long-lived host-side
session so tests and mid-flight questions keep working; once the Run is
terminal the scheduling path is used.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from enterprise_agent_platform.contracts.commands import FollowupCommand
from enterprise_agent_platform.contracts.enums import RunState
from enterprise_agent_platform.contracts.models import (
    FollowupAnswer,
    FollowupHistoryPage,
    FollowupRecord,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

TERMINAL_RUN_STATES = frozenset({RunState.SUCCEEDED, RunState.FAILED})


class FollowupError(PlatformError):
    """Follow-up specific failures (timeouts, busy runs)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(code, message, retryable=retryable)


class FollowupService:
    """Route follow-ups: terminal Runs via a fresh Attempt, live Runs inline."""

    def __init__(
        self,
        store: PlatformStore,
        control: ControlPlaneService | None = None,
        sessions: RunSessionProvider | None = None,
        *,
        poll_interval_seconds: float = 1.0,
        answer_timeout_seconds: float = 300.0,
    ) -> None:
        self._store = store
        self._control = control or ControlPlaneService(store)
        self._sessions = sessions
        self._handles: dict[str, object] = {}
        self._poll_interval_seconds = poll_interval_seconds
        self._answer_timeout_seconds = answer_timeout_seconds
        self._answers: dict[tuple[str, str], FollowupAnswer] = {}
        self._records: dict[str, list[FollowupRecord]] = {}
        self._next_seq: dict[str, int] = {}

    # ── Public API ─────────────────────────────────────────────────────────

    async def followup(
        self,
        ctx: RequestContext,
        run_id: str,
        command: FollowupCommand,
        idempotency_key: str,
    ) -> FollowupAnswer:
        cached = self._answers.get((run_id, idempotency_key))
        if cached is not None:
            return cached
        # Ensure the Run exists; PlatformError NOT_FOUND -> HTTP 404.
        run = await self._store.get_run(ctx.tenant_id, run_id)

        if run.status in TERMINAL_RUN_STATES:
            answer = await self._followup_via_new_attempt(
                ctx, run_id, command, idempotency_key
            )
        else:
            answer = await self._followup_inline(ctx, run_id, command)
        self._answers[(run_id, idempotency_key)] = answer
        return answer

    async def list_followups(
        self,
        ctx: RequestContext,
        run_id: str,
    ) -> FollowupHistoryPage:
        # Ensure the Run exists (will raise if not).
        await self._store.get_run(ctx.tenant_id, run_id)
        # Durable records are the authoritative history (PENDING + ANSWERED
        # survive restarts; SDD §13.1 aggregation gap). In-memory records only
        # supplement inline answers that never touched the durable store, and
        # are deduped by client_followup_id (the scheduling path persists the
        # record AND caches it in memory — without the dedupe every answered
        # follow-up would appear twice).
        rows: list[tuple[datetime, str, str | None, str, str]] = []
        seen: set[str] = set()
        for record in await self._list_all(ctx.tenant_id, run_id):
            ordered_at = (
                record.answered_at if record.answered_at is not None else record.created_at
            )
            rows.append(
                (ordered_at, record.question, record.answer, record.client_followup_id, record.status)
            )
            seen.add(record.client_followup_id)
        for record in self._records.get(run_id, ()):
            if record.client_followup_id in seen:
                continue
            rows.append(
                (record.answered_at, record.question, record.answer, record.client_followup_id, "ANSWERED")
            )
        rows.sort(key=lambda row: row[0])
        records = tuple(
            FollowupRecord(
                schema_version="followup-record/v1",
                run_id=run_id,
                followup_seq=index,
                question=row[1],
                answer=row[2],
                answered_at=row[0] if row[4] == "ANSWERED" else None,
                client_followup_id=row[3],
                status=row[4],
            )
            for index, row in enumerate(rows)
        )
        return FollowupHistoryPage(
            schema_version="followup-history-page/v1",
            run_id=run_id,
            total_count=len(records),
            records=records,
        )

    # ── Terminal-Run path: fresh Attempt + Runner ─────────────────────────

    async def _followup_via_new_attempt(
        self,
        ctx: RequestContext,
        run_id: str,
        command: FollowupCommand,
        idempotency_key: str,
    ) -> FollowupAnswer:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._answer_timeout_seconds

        # Serialise concurrent follow-ups: wait until the Run is terminal again
        # and no earlier PENDING follow-up is still queued.
        while True:
            current_run = await self._store.get_run(ctx.tenant_id, run_id)
            pending = await self._list_pending(ctx.tenant_id, run_id)
            if current_run.status in TERMINAL_RUN_STATES and not pending:
                break
            if loop.time() >= deadline:
                raise FollowupError(
                    "FOLLOWUP_BUSY",
                    "run is busy or has a pending follow-up; try again later",
                    retryable=True,
                )
            await asyncio.sleep(self._poll_interval_seconds)

        followup = await self._control.queue_followup(
            ctx,
            run_id,
            question=command.question,
            client_followup_id=command.client_followup_id,
        )

        # Poll until the fresh Attempt's Runner commits an answer.
        while True:
            current = await self._store.get_followup_request(
                ctx.tenant_id, followup.followup_id
            )
            if current.status == "ANSWERED" and current.answer is not None:
                result = FollowupAnswer(
                    schema_version="followup-answer/v1",
                    run_id=run_id,
                    session_id=f"followup:{followup.followup_id}",
                    question=command.question,
                    answer=current.answer,
                )
                self._remember(run_id, command, current.answer, current.answered_at)
                return result
            if loop.time() >= deadline:
                raise FollowupError(
                    "FOLLOWUP_TIMEOUT",
                    f"follow-up answer not produced within "
                    f"{self._answer_timeout_seconds:.0f}s",
                    retryable=True,
                )
            await asyncio.sleep(self._poll_interval_seconds)

    # ── Live-Run path: inline host-side session (legacy behaviour) ─────────

    async def _followup_inline(
        self,
        ctx: RequestContext,
        run_id: str,
        command: FollowupCommand,
    ) -> FollowupAnswer:
        if self._sessions is None:
            raise FollowupError(
                "FOLLOWUP_UNAVAILABLE",
                "no session provider is configured for live follow-ups",
            )
        handle = self._handles.get(run_id)
        if handle is None:
            handle = await self._sessions.open(
                run_id=run_id,
                intent="Follow-up question",
                resource_refs=(),
                host_context_ref=None,
            )
            self._handles[run_id] = handle
        answer = await self._sessions.followup(handle, command.question, read_only=True)
        result = FollowupAnswer(
            schema_version="followup-answer/v1",
            run_id=run_id,
            session_id=str(getattr(handle, "session_id", "")),
            question=command.question,
            answer=answer,
        )
        self._remember(run_id, command, answer, datetime.now(UTC))
        return result

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _list_pending(
        self, tenant_id: str, run_id: str
    ) -> tuple[object, ...]:
        try:
            requests = await self._store.list_followup_requests(tenant_id, run_id)
        except AttributeError:
            return ()
        return tuple(item for item in requests if item.status == "PENDING")

    async def _list_all(
        self, tenant_id: str, run_id: str
    ) -> tuple[object, ...]:
        """All durable follow-up records for the Run (PENDING + ANSWERED)."""
        try:
            return await self._store.list_followup_requests(tenant_id, run_id)
        except AttributeError:
            return ()

    def _remember(
        self,
        run_id: str,
        command: FollowupCommand,
        answer: str,
        answered_at: datetime,
    ) -> None:
        seq = self._next_seq.setdefault(run_id, 0)
        self._records.setdefault(run_id, []).append(
            FollowupRecord(
                schema_version="followup-record/v1",
                run_id=run_id,
                followup_seq=seq,
                question=command.question,
                answer=answer,
                answered_at=answered_at,
                client_followup_id=command.client_followup_id,
            )
        )
        self._next_seq[run_id] = seq + 1


__all__ = ["FollowupError", "FollowupService"]