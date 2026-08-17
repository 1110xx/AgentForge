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
    details: dict[str, str] = Field(default_factory=dict)


class InvalidTransitionError(ValueError):
    pass
