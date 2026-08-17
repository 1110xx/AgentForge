"""Short-lived Artifact download authorization with host policy and fail-closed audit."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from enterprise_agent_platform.contracts.enums import ArtifactVersionState
from enterprise_agent_platform.contracts.models import ArtifactDownloadAuthorization
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.domain.records import ArtifactVersionRecord, AuditEventRecord
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore


class ArtifactAccessPolicy(Protocol):
    async def authorize(self, ctx: RequestContext, artifact: ArtifactVersionRecord) -> None: ...


class ArtifactDownloadSigner(Protocol):
    async def sign_download(
        self,
        *,
        object_uri: str,
        authorization_id: str,
        expires_at: datetime,
        media_type: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ArtifactDownloadRequest:
    run_id: str
    artifact_id: str
    version: int


class ArtifactDownloadService:
    def __init__(
        self,
        *,
        store: PlatformStore,
        policy: ArtifactAccessPolicy,
        signer: ArtifactDownloadSigner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(minutes=5),
        allowed_url_schemes: tuple[str, ...] = ("https",),
    ) -> None:
        if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
            raise ValueError("download authorization ttl must be at most 15 minutes")
        if not allowed_url_schemes:
            raise ValueError("at least one download URL scheme is required")
        self._store = store
        self._policy = policy
        self._signer = signer
        self._clock = clock
        self._ttl = ttl
        self._schemes = frozenset(allowed_url_schemes)

    async def authorize(
        self, ctx: RequestContext, request: ArtifactDownloadRequest
    ) -> ArtifactDownloadAuthorization:
        if request.version < 1:
            raise PlatformError("INVALID_ARTIFACT_VERSION", "artifact version is invalid")
        artifact = await self._store.get_artifact_version(
            ctx.tenant_id, request.artifact_id, request.version
        )
        self._require_ready_for_run(artifact, request.run_id)
        await self._policy.authorize(ctx, artifact)

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("artifact download clock must be timezone-aware")
        now = now.astimezone(UTC)
        expires_at = now + self._ttl
        authorization_id = self._store.new_id("artifact_download")
        url = await self._signer.sign_download(
            object_uri=artifact.object_uri,
            authorization_id=authorization_id,
            expires_at=expires_at,
            media_type=artifact.media_type,
        )
        self._validate_url(url)

        async with self._store.transaction() as tx:
            run = await tx.lock_run(ctx.tenant_id, request.run_id)
            current = await tx.get_artifact_version(
                ctx.tenant_id, request.artifact_id, request.version
            )
            self._require_ready_for_run(current, request.run_id)
            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ctx.tenant_id,
                    audit_event_id=self._store.new_id("audit"),
                    run_id=request.run_id,
                    actor_id=ctx.actor_id,
                    action="artifact.download.authorized",
                    entity_type="artifact_version",
                    entity_id=request.artifact_id,
                    entity_version=current.state_version,
                    outcome="SUCCEEDED",
                    trace_id=ctx.trace_id,
                    details={
                        "authorization_id": authorization_id,
                        "artifact_id": request.artifact_id,
                        "version": request.version,
                        "expires_at": expires_at.isoformat(),
                        "run_version": run.version,
                    },
                    created_at=now,
                )
            )

        return ArtifactDownloadAuthorization(
            schema_version="artifact-download-authorization/v1",
            authorization_id=authorization_id,
            artifact_id=request.artifact_id,
            version=request.version,
            download_url=url,
            expires_at=expires_at,
        )

    @staticmethod
    def _require_ready_for_run(artifact: ArtifactVersionRecord, run_id: str) -> None:
        if (
            artifact.run_id != run_id
            or artifact.state is not ArtifactVersionState.READY
            or artifact.ready_at is None
        ):
            raise PlatformError("NOT_FOUND", "artifact version was not found")

    def _validate_url(self, value: str) -> None:
        if len(value) > 8192:
            raise PlatformError("INVALID_DOWNLOAD_AUTHORIZATION", "download URL is invalid")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in self._schemes
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise PlatformError("INVALID_DOWNLOAD_AUTHORIZATION", "download URL is invalid")


__all__ = [
    "ArtifactAccessPolicy",
    "ArtifactDownloadRequest",
    "ArtifactDownloadService",
    "ArtifactDownloadSigner",
]
