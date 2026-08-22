"""Safe public errors and pure domain transition errors."""
from typing import Literal

from pydantic import Field

from .models import StrictModel


class ApiErrorEnvelope(StrictModel):
    schema_version: Literal["api-error/v1"]
    code: str
    message: str
    trace_id: str | None = None
    retryable: bool = False
    # JSON-serialisable detail payload (validation errors carry their loc/msg
    # records; callers must keep values JSON-safe).
    details: dict[str, object] = Field(default_factory=dict)


class InvalidTransitionError(ValueError):
    pass
