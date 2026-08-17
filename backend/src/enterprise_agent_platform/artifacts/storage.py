"""Async object-store ports with a portable local and S3-compatible adapter."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from enterprise_agent_platform.persistence.protocol import PlatformError


class ObjectStore(Protocol):
    async def put(self, key: str, content: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def delete(self, key: str) -> None: ...


def _safe_key(key: str) -> PurePosixPath:
    if not key or "\x00" in key or "\\" in key:
        raise PlatformError("INVALID_OBJECT_KEY", "object key is invalid")
    path = PurePosixPath(key)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PlatformError("INVALID_OBJECT_KEY", "object key is invalid")
    return path


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        relative = _safe_key(key)
        target = self._root.joinpath(*relative.parts)
        if self._root not in target.parents:
            raise PlatformError("INVALID_OBJECT_KEY", "object key is invalid")
        return target

    async def put(self, key: str, content: bytes) -> None:
        target = self._path(key)

        def write_once() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as error:
                raise PlatformError(
                    "IMMUTABLE_OBJECT_EXISTS", "object keys cannot be overwritten"
                ) from error
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

        await asyncio.to_thread(write_once)

    async def get(self, key: str) -> bytes:
        target = self._path(key)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError as error:
            raise PlatformError("OBJECT_NOT_FOUND", "object does not exist") from error

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> None:
        target = self._path(key)

        def remove() -> None:
            try:
                target.unlink()
            except FileNotFoundError:
                return

        await asyncio.to_thread(remove)


class S3SyncClient(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...
    def get_object(self, **kwargs: Any) -> Any: ...
    def head_object(self, **kwargs: Any) -> Any: ...
    def delete_object(self, **kwargs: Any) -> Any: ...


class S3ObjectStore:
    def __init__(
        self,
        client: S3SyncClient,
        *,
        bucket: str,
        prefix: str = "agent-platform",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._timeout = timeout_seconds

    def _object_key(self, key: str) -> str:
        safe = _safe_key(key).as_posix()
        return f"{self._prefix}/{safe}" if self._prefix else safe

    async def _call(self, function: Any, **kwargs: Any) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(function, **kwargs), timeout=self._timeout
        )

    async def put(self, key: str, content: bytes) -> None:
        await self._call(
            self._client.put_object,
            Bucket=self._bucket,
            Key=self._object_key(key),
            Body=content,
            IfNoneMatch="*",
        )

    async def get(self, key: str) -> bytes:
        response = await self._call(
            self._client.get_object, Bucket=self._bucket, Key=self._object_key(key)
        )
        body = response["Body"]
        return await asyncio.wait_for(asyncio.to_thread(body.read), timeout=self._timeout)

    async def exists(self, key: str) -> bool:
        try:
            await self._call(
                self._client.head_object, Bucket=self._bucket, Key=self._object_key(key)
            )
        except Exception as error:
            status = (
                getattr(error, "response", {})
                .get("ResponseMetadata", {})
                .get("HTTPStatusCode")
            )
            if status == 404:
                return False
            raise
        return True

    async def delete(self, key: str) -> None:
        await self._call(
            self._client.delete_object, Bucket=self._bucket, Key=self._object_key(key)
        )
