"""Generation-fenced immutable Artifact and WorkspaceSnapshot services."""
from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal, Protocol

from enterprise_agent_platform.artifacts.storage import ObjectStore
from enterprise_agent_platform.persistence.protocol import PlatformError

ArtifactVersionState = Literal["PREPARING", "READY", "FAILED"]


@dataclass(frozen=True, slots=True)
class ScanResult:
    clean: bool
    scanner_version: str
    reason_code: str | None = None


class ArtifactScanner(Protocol):
    async def scan(self, content: bytes, *, digest: str) -> ScanResult: ...


@dataclass(frozen=True, slots=True)
class ArtifactVersionRecord:
    tenant_id: str
    run_id: str
    execution_unit_id: str
    source_attempt_id: str
    artifact_id: str
    logical_name: str
    classification: str
    version: int
    generation: int
    state: ArtifactVersionState
    object_key: str
    checksum: str
    size_bytes: int
    scanner_version: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime


class ArtifactRepository(Protocol):
    async def reserve(
        self,
        *,
        tenant_id: str,
        run_id: str,
        execution_unit_id: str,
        source_attempt_id: str,
        artifact_id: str,
        logical_name: str,
        classification: str,
        generation: int,
        object_key: str,
        checksum: str,
        size_bytes: int,
        requested_version: int | None,
    ) -> ArtifactVersionRecord: ...

    async def finalize(
        self,
        record: ArtifactVersionRecord,
        *,
        state: Literal["READY", "FAILED"],
        scanner_version: str | None,
        failure_code: str | None,
    ) -> ArtifactVersionRecord: ...

    async def get(
        self, tenant_id: str, artifact_id: str, version: int
    ) -> ArtifactVersionRecord | None: ...


class InMemoryArtifactRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str, int], ArtifactVersionRecord] = {}
        self._active_generations: dict[tuple[str, str], int] = {}

    async def set_active_generation(
        self, tenant_id: str, execution_unit_id: str, generation: int
    ) -> None:
        async with self._lock:
            self._active_generations[(tenant_id, execution_unit_id)] = generation

    async def reserve(
        self,
        *,
        tenant_id: str,
        run_id: str,
        execution_unit_id: str,
        source_attempt_id: str,
        artifact_id: str,
        logical_name: str,
        classification: str,
        generation: int,
        object_key: str,
        checksum: str,
        size_bytes: int,
        requested_version: int | None,
    ) -> ArtifactVersionRecord:
        async with self._lock:
            if self._active_generations.get((tenant_id, execution_unit_id)) != generation:
                raise PlatformError("STALE_GENERATION", "generation is not active")
            versions = [
                version
                for (record_tenant, record_artifact, version) in self._records
                if record_tenant == tenant_id and record_artifact == artifact_id
            ]
            version = (
                requested_version
                if requested_version is not None
                else max(versions, default=0) + 1
            )
            key = (tenant_id, artifact_id, version)
            if key in self._records:
                raise PlatformError(
                    "IMMUTABLE_ARTIFACT_VERSION", "artifact versions cannot be overwritten"
                )
            now = datetime.now(UTC)
            record = ArtifactVersionRecord(
                tenant_id=tenant_id,
                run_id=run_id,
                execution_unit_id=execution_unit_id,
                source_attempt_id=source_attempt_id,
                artifact_id=artifact_id,
                logical_name=logical_name,
                classification=classification,
                version=version,
                generation=generation,
                state="PREPARING",
                object_key=object_key,
                checksum=checksum,
                size_bytes=size_bytes,
                scanner_version=None,
                failure_code=None,
                created_at=now,
                updated_at=now,
            )
            self._records[key] = record
            return record

    async def finalize(
        self,
        record: ArtifactVersionRecord,
        *,
        state: Literal["READY", "FAILED"],
        scanner_version: str | None,
        failure_code: str | None,
    ) -> ArtifactVersionRecord:
        async with self._lock:
            key = (record.tenant_id, record.artifact_id, record.version)
            current = self._records.get(key)
            if current != record or current.state != "PREPARING":
                raise PlatformError("VERSION_CONFLICT", "artifact version changed")
            finalized = replace(
                current,
                state=state,
                scanner_version=scanner_version,
                failure_code=failure_code,
                updated_at=datetime.now(UTC),
            )
            self._records[key] = finalized
            return finalized

    async def get(
        self, tenant_id: str, artifact_id: str, version: int
    ) -> ArtifactVersionRecord | None:
        async with self._lock:
            return self._records.get((tenant_id, artifact_id, version))


class ArtifactService:
    def __init__(
        self,
        object_store: ObjectStore,
        repository: ArtifactRepository,
        scanner: ArtifactScanner,
    ) -> None:
        self._objects = object_store
        self._repository = repository
        self._scanner = scanner

    async def publish(
        self,
        *,
        tenant_id: str,
        run_id: str,
        execution_unit_id: str,
        source_attempt_id: str,
        artifact_id: str,
        logical_name: str,
        classification: str,
        content: bytes,
        expected_generation: int,
        requested_version: int | None = None,
    ) -> ArtifactVersionRecord:
        if expected_generation < 1:
            raise PlatformError("STALE_GENERATION", "generation must be active")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        unique = uuid.uuid4().hex
        object_key = f"tenants/{tenant_id}/runs/{run_id}/artifacts/{artifact_id}/staged/{unique}"
        reserved = await self._repository.reserve(
            tenant_id=tenant_id,
            run_id=run_id,
            execution_unit_id=execution_unit_id,
            source_attempt_id=source_attempt_id,
            artifact_id=artifact_id,
            logical_name=logical_name,
            classification=classification,
            generation=expected_generation,
            object_key=object_key,
            checksum=digest,
            requested_version=requested_version,
            size_bytes=len(content),
        )
        try:
            await self._objects.put(object_key, content)
            stored = await self._objects.get(object_key)
            stored_digest = f"sha256:{hashlib.sha256(stored).hexdigest()}"
            if stored_digest != digest or len(stored) != len(content):
                return await self._repository.finalize(
                    reserved,
                    state="FAILED",
                    scanner_version=None,
                    failure_code="CHECKSUM_MISMATCH",
                )
            scan = await self._scanner.scan(stored, digest=digest)
            return await self._repository.finalize(
                reserved,
                state="READY" if scan.clean else "FAILED",
                scanner_version=scan.scanner_version,
                failure_code=None if scan.clean else (scan.reason_code or "SCAN_REJECTED"),
            )
        except BaseException:
            current = await self._repository.get(tenant_id, artifact_id, reserved.version)
            if current == reserved:
                await self._repository.finalize(
                    reserved,
                    state="FAILED",
                    scanner_version=None,
                    failure_code="STAGING_FAILED",
                )
            raise


SnapshotEntryKind = Literal["file", "directory", "symlink", "fifo", "device", "hardlink"]


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    kind: SnapshotEntryKind
    size: int
    checksum: str | None = None
    object_ref: str | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    entries: tuple[ManifestEntry, ...]
    schema_version: str = "workspace-snapshot-manifest/v1"


@dataclass(frozen=True, slots=True)
class ValidatedSnapshotManifest:
    entries: tuple[ManifestEntry, ...]
    total_bytes: int


def _validate_relative_path(raw_path: str) -> PurePosixPath:
    if not raw_path or "\x00" in raw_path or "\\" in raw_path or PureWindowsPath(raw_path).drive:
        raise PlatformError("INVALID_SNAPSHOT_PATH", "snapshot path is invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PlatformError("INVALID_SNAPSHOT_PATH", "snapshot path is invalid")
    return path


def validate_snapshot_manifest(
    manifest: SnapshotManifest,
    max_entries: int = 100_000,
    max_total_bytes: int = 10 * 1024 * 1024 * 1024,
) -> ValidatedSnapshotManifest:
    if manifest.schema_version != "workspace-snapshot-manifest/v1":
        raise PlatformError("UNSUPPORTED_SNAPSHOT_SCHEMA", "snapshot schema is unsupported")
    if len(manifest.entries) > max_entries:
        raise PlatformError("SNAPSHOT_LIMIT_EXCEEDED", "snapshot contains too many entries")
    seen: set[str] = set()
    total = 0
    for entry in manifest.entries:
        path = _validate_relative_path(entry.path)
        normalized = path.as_posix()
        if normalized in seen:
            raise PlatformError("DUPLICATE_SNAPSHOT_PATH", "snapshot path is duplicated")
        seen.add(normalized)
        if entry.size < 0:
            raise PlatformError("INVALID_SNAPSHOT_SIZE", "snapshot size is invalid")
        if entry.kind not in ("file", "directory", "symlink", "fifo", "device", "hardlink"):
            raise PlatformError("UNSUPPORTED_SNAPSHOT_ENTRY", "snapshot entry type is unsupported")
        if entry.kind == "file":
            total += entry.size
        elif entry.size != 0:
            raise PlatformError("INVALID_SNAPSHOT_SIZE", "non-file size must be zero")
        if entry.kind == "symlink":
            if entry.link_target is None:
                raise PlatformError("INVALID_SNAPSHOT_SYMLINK", "symlink target is required")
            target = entry.link_target
            if "\x00" in target or "\\" in target or PurePosixPath(target).is_absolute():
                raise PlatformError("INVALID_SNAPSHOT_SYMLINK", "symlink target is invalid")
            resolved = posixpath.normpath(posixpath.join(path.parent.as_posix(), target))
            if resolved == ".." or resolved.startswith("../"):
                raise PlatformError(
                    "ESCAPING_SNAPSHOT_SYMLINK", "symlink target escapes the snapshot"
                )
    if total > max_total_bytes:
        raise PlatformError("SNAPSHOT_LIMIT_EXCEEDED", "snapshot is too large")
    return ValidatedSnapshotManifest(entries=manifest.entries, total_bytes=total)


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotRecord:
    tenant_id: str
    run_id: str
    snapshot_id: str
    generation: int
    state: Literal["READY", "FAILED"]
    object_key: str
    checksum: str
    total_bytes: int


class WorkspaceSnapshotService:
    def __init__(self, object_store: ObjectStore) -> None:
        self._objects = object_store
        self._records: dict[tuple[str, str], WorkspaceSnapshotRecord] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        *,
        tenant_id: str,
        run_id: str,
        snapshot_id: str,
        manifest: SnapshotManifest,
        generation: int,
    ) -> WorkspaceSnapshotRecord:
        if generation < 1:
            raise PlatformError("STALE_GENERATION", "generation must be active")
        validated = validate_snapshot_manifest(manifest)
        payload = json.dumps(
            {
                "schema_version": manifest.schema_version,
                "entries": [
                    {
                        "path": entry.path,
                        "kind": entry.kind,
                        "size": entry.size,
                        "checksum": entry.checksum,
                        "object_ref": entry.object_ref,
                        "link_target": entry.link_target,
                    }
                    for entry in manifest.entries
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        key = f"tenants/{tenant_id}/runs/{run_id}/snapshots/{snapshot_id}/{digest[7:]}"
        async with self._lock:
            if (tenant_id, snapshot_id) in self._records:
                raise PlatformError(
                    "IMMUTABLE_WORKSPACE_SNAPSHOT", "workspace snapshots cannot be overwritten"
                )
            await self._objects.put(key, payload)
            stored = await self._objects.get(key)
            if f"sha256:{hashlib.sha256(stored).hexdigest()}" != digest:
                state: Literal["READY", "FAILED"] = "FAILED"
            else:
                state = "READY"
            record = WorkspaceSnapshotRecord(
                tenant_id=tenant_id,
                run_id=run_id,
                snapshot_id=snapshot_id,
                generation=generation,
                state=state,
                object_key=key,
                checksum=digest,
                total_bytes=validated.total_bytes,
            )
            self._records[(tenant_id, snapshot_id)] = record
            return record

    async def restore(self, tenant_id: str, snapshot_id: str) -> bytes:
        async with self._lock:
            record = self._records.get((tenant_id, snapshot_id))
            if record is None:
                raise PlatformError("NOT_FOUND", "workspace snapshot does not exist")
            if record.state != "READY":
                raise PlatformError("SNAPSHOT_NOT_READY", "workspace snapshot is not ready")
            payload = await self._objects.get(record.object_key)
            if f"sha256:{hashlib.sha256(payload).hexdigest()}" != record.checksum:
                raise PlatformError("CHECKSUM_MISMATCH", "workspace snapshot is corrupted")
            return payload
