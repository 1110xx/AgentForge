"""Browser-facing command envelopes that deliberately omit server authority."""
import re
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .models import StrictModel

AUTHORITY_KEY_TOKENS = frozenset({
    "audience",
    "auth",
    "authentication",
    "authorization",
    "bearer",
    "credential",
    "grant",
    "identity",
    "key",
    "oauth",
    "owner",
    "password",
    "passwd",
    "permission",
    "principal",
    "pwd",
    "role",
    "scope",
    "secret",
    "target",
    "tenant",
    "token",
    "tool",
    "uri",
    "url",
})

COMPACT_AUTHORITY_KEY_ALIASES = frozenset({
    "accesskey",
    "accesssecret",
    "accesstoken",
    "apicredential",
    "apikey",
    "apisecret",
    "authkey",
    "authtoken",
    "bearertoken",
    "clientcredential",
    "clientcredentials",
    "clientkey",
    "clientsecret",
    "clienttoken",
    "encryptionkey",
    "passwordhash",
    "privatekey",
    "publickey",
    "refreshtoken",
    "secretkey",
    "signingkey",
})


class SyntheticAnalysisDisplayOptions(StrictModel):
    theme: Literal["compact", "comfortable"]
    labels: dict[str, str] = Field(default_factory=dict)


class SyntheticAnalysisOptions(StrictModel):
    display: tuple[SyntheticAnalysisDisplayOptions, ...] = ()


class SyntheticAnalysisParameters(StrictModel):
    analysis_mode: Literal["summary", "thorough", "failure-pattern"] | None = None
    max_items: Annotated[int, Field(ge=1, le=1000)] | None = None
    options: SyntheticAnalysisOptions | None = None


WORKFLOW_PARAMETER_MODELS: dict[str, type[StrictModel]] = {
    "synthetic-analysis": SyntheticAnalysisParameters,
}


def _key_tokens(key: str) -> set[str]:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    raw_tokens = set(re.findall(r"[a-z0-9]+", snake_case.lower()))
    return raw_tokens | {token[:-1] for token in raw_tokens if token.endswith("s")}


def _is_authority_key(key: str) -> bool:
    compact_key = re.sub(r"[^a-z0-9]", "", key.lower())
    compact_candidates = {compact_key}
    if compact_key.endswith("s"):
        compact_candidates.add(compact_key[:-1])
    return not compact_candidates.isdisjoint(COMPACT_AUTHORITY_KEY_ALIASES) or bool(
        _key_tokens(key) & AUTHORITY_KEY_TOKENS
    )


def _contains_authority_key(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return any(
            _is_authority_key(key) or _contains_authority_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_authority_key(item) for item in value)
    return False


class CreateRunCommand(StrictModel):
    workflow_type: str
    intent: str
    resource_refs: Annotated[tuple[str, ...], Field(min_length=1)]
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    host_context_ref: str | None = None

    @model_validator(mode="after")
    def validate_controlled_parameters(self) -> "CreateRunCommand":
        if _contains_authority_key(self.parameters):
            raise ValueError("parameters cannot contain authority-shaped keys")
        parameter_model = WORKFLOW_PARAMETER_MODELS.get(self.workflow_type)
        if parameter_model is None:
            if self.parameters:
                raise ValueError(f"{self.workflow_type} does not accept parameters")
        else:
            parameter_model.model_validate(self.parameters)
        return self


class UiActionCommand(StrictModel):
    run_id: str
    surface_id: str
    surface_revision: Annotated[int, Field(ge=1)]
    action_ref: str
    client_action_id: str
    displayed_digest: str | None = None
    host_context_ref: str | None = None


class FollowupCommand(StrictModel):
    run_id: str
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    client_followup_id: str


class EffectGrantRequest(StrictModel):
    """Server-side request to authorise and dispatch one prepared Effect.

    Carries only the facts frozen into the Effect ledger row; the caller is
    expected to hold a service identity that the capability issuer binds to the
    tenant/effect pair. Used by the internal execute route to mint the
    tenant/effect-bound capability token handed to the DurableEffectExecutor.
    """

    tenant_id: str
    run_id: str
    effect_id: str
    approval_id: str
    request_digest: str
    tool_name: str
    tool_version: str
    tool_spec_digest: str
    connector_name: str
    canonical_target: str
    required_scopes: tuple[str, ...]
    ttl_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
